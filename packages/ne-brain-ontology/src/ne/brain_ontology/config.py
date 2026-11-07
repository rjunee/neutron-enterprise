# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``OntologySettings`` — the Brain-T3 module configuration (NE-085a).

An :class:`~ne.contracts.config.NeSettings` subclass (the ``BrainGraphSettings``
convention): values layer init > ``NE_ONTOLOGY_`` env (``__`` nesting) > TOML >
defaults.
Selects the backend (``oxigraph`` no-docker dev / ``fuseki`` prod), the data
graph the gateway writes into, the quarantine lane location, and the MRSW write
retry bounds carried over from the ABI adapter's operational knowledge.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import NoDecode, SettingsConfigDict

from ne.contracts.config import NeSettings

__all__ = ["OntologySettings"]


class OntologySettings(NeSettings):
    """Configuration for the Tier-3 ontology store + SHACL gateway (opt-in)."""

    model_config = SettingsConfigDict(
        env_prefix="NE_ONTOLOGY_",
        env_nested_delimiter="__",
        extra="forbid",
        toml_file=Path("ne.toml"),
    )

    backend: Literal["oxigraph", "fuseki"] = "oxigraph"
    """Which triple-store backend the gateway wraps."""

    oxigraph_path: str | None = None
    """Filesystem path for the embedded oxigraph store (``None`` → in-memory)."""

    fuseki_url: str | None = None
    """The Fuseki DATASET endpoint (e.g. ``http://fuseki:3030/ne``)."""

    fuseki_user: SecretStr | None = None
    fuseki_password: SecretStr | None = None

    data_graph_iri: str = "urn:ne:data:default"
    """Named graph every validated instance-data write lands in."""

    quarantine_db: Path | None = None
    """Sqlite path for the quarantine lane (``None`` → in-memory)."""

    actor: str = "ne:brain-ontology:gateway"
    """Actor stamped on emitted ``ShapeViolation`` events."""

    include_ontology_graph: bool = True
    """Load the vendored BFO/CCO corpus as the pySHACL ont-graph on every write."""

    write_retry_attempts: int = Field(default=5, ge=0)
    """MRSW retry attempts for transient Fuseki 500/503 write-lock contention."""

    write_retry_base_delay_s: float = Field(default=0.1, ge=0.0)
    """Base backoff delay (seconds) for the MRSW write retry."""

    # -- NE-085c verified-answers (governed SPARQL templates) ------------------

    verified_answers_enabled: bool = False
    """Opt-in: expose the curated SPARQL templates as governed C6 tools.

    Default OFF — when disabled the platform's tool menu, guardrail policy map, and
    frozen ``config_hash`` are byte-identical to pre-085c (brain-tier3-spec §11.2).
    """

    verified_templates_dir: Path | None = None
    """Directory of the per-customer verified-answer template TOMLs (config bundle).

    The package ships NO production templates — they are per-customer config-bundle
    content (git). ``None`` while :attr:`verified_answers_enabled` is ``True`` is a
    fail-loud boot error, never a silent empty registry.
    """

    verified_max_rows: int = Field(default=500, ge=1, le=10000)
    """Trusted-code row cap enforced on every verified-answer read (LIMIT cap+1)."""

    verified_max_result_tokens: int | None = Field(default=None, ge=1)
    """DEFAULT-OFF token cap on the MODEL-FACING verified-read result (§4.2, R2-P1-4).

    Env ``NE_ONTOLOGY_VERIFIED_MAX_RESULT_TOKENS`` (via the ``env_prefix`` above).
    ``None`` ⇒ token packing is disabled EVERYWHERE — the app→model result is
    byte-identical to pre-CTXB-5 (row-cap-only) truncation behavior, so merging this
    unit regresses no currently-green beat. When set, ``execute_template`` packs rows
    in query order under the shared ``ByteTokenEstimator`` until the budget is hit
    (app→model bound; the row cap remains the store→app bound). Production enables it
    in NE-CTXB-8. As with ``DispatchSettings``, this field is a ``config_hash`` input
    only for verified-enabled deployments (platform stamps ``OntologySettings`` only
    when the T3 read surface is on) — an enabled deployment's hash moves when set."""

    # -- NE-GA-G5 grounded agent --

    grounded_agent_enabled: bool = False
    """Opt-in: turn the grounded agent ON end-to-end.

    Default OFF — when disabled ``build_platform`` never constructs a
    ``GroundingPolicy``, so default-off deployments are byte-identical
    (including ``config_hash``, which stamps ``OntologySettings`` only when the
    T3 read surface is enabled — see ``platform.py`` around line 466-467).
    """

    grounded_agent_name: str = "grounded-analyst"
    """The ONE chat ``agent_name`` whose sessions receive the grounding
    post-step + verified-reads-only toolset restriction.

    Every other ``agent_name`` is untouched — ordinary agents never see a
    ``GroundingPolicy``.
    """

    grounded_run_agent_ids: Annotated[tuple[str, ...], NoDecode] = ()
    """NE-GA-RUN1: the canonical-Run Agent ids whose agentic executions get the
    whole-answer grounding gate (the same ``GroundingPolicy`` the chat grounded
    agent composes, with the G4 assist structurally stripped on the Run path).

    Env ``NE_ONTOLOGY_GROUNDED_RUN_AGENT_IDS`` — comma-separated agent ids.
    Default EMPTY: no Run is gated and the Run execution path is byte-identical.
    Deployment config, NEVER caller/request-selectable (B-P0-1). Scope it to
    Agents whose source-bearing grants are verified templates (``read.kg.*``):
    the gate's corpus is built ONLY from recorded VerifiedAnswers, so an Agent
    that legitimately answers from ``read.knowledge.search`` chunks would have
    every such answer withheld — a mis-scoped gate, not a stricter one.
    """

    grounded_assist_enabled: bool = True
    """Enable the G4 bounded one-pass model assist.

    The assist is a bare gateway completion, task-tagged
    ``grounding-assist``, given NO tool catalog. ``False`` disables it
    entirely, leaving a pure hard-gate (deterministic fallback only).
    """

    # -- NE-HGATE-3 grounding strictness (docs/architecture/anti-hallucination-grounding.md §7) --

    grounding_strictness: Literal["strict", "standard"] = "strict"
    """The deployment strictness tier (design §2). Env
    ``NE_ONTOLOGY_GROUNDING_STRICTNESS``.

    Default ``strict`` preserves today's posture on upgrade (B-P1-2) — a version
    bump never silently loosens an existing grounded deployment's gate.
    ``standard`` is the RECOMMENDED posture for conversational deployments: it
    only ever moves NON-factual vocabulary; numerals (digit or spelled), IRIs,
    citations, and fact-sentence entity identity verify at EVERY tier (design
    §2.1) — the knob can never tier a fact class. This is deployment config,
    NEVER caller/request-selectable (B-P0-1: a per-request tier would be a
    self-service assurance downgrade). Like ``verified_max_result_tokens``
    (above), it is a ``config_hash`` input only when the T3 read surface is on.
    """

    grounding_run_strictness: Literal["strict", "standard"] = "strict"
    """The strictness tier for the canonical-Run grounding gate (NE-GA-RUN4).
    Env ``NE_ONTOLOGY_GROUNDING_RUN_STRICTNESS``.

    The Run gate previously INHERITED ``grounding_strictness`` — the ONE shared
    chat policy's tier — so a deployment at chat-``standard`` silently ran its
    gated Run agents at ``standard`` too. This knob makes the Run tier its own
    deployment decision, and its default is ``strict`` DELIBERATELY independent
    of the chat knob: a conversational-posture choice must never loosen the Run
    gate silently. The honest cost is low precisely because
    ``grounded_run_agent_ids`` is scoped to agents whose whole retrievable
    corpus is gate-visible (verified ``read.kg.*`` templates only — see that
    field's docstring).

    SCOPE OF THE KNOB (U11 review round 2): a tier only moves which finding
    CLASSES enforce, so it governs VOCABULARY enforcement and nothing else —
    no tier can make "empty-corpus turns always withhold" true, because a
    draft composed entirely of core-allowlisted words yields zero findings at
    every tier. That guarantee is instead the Run execution path's own
    NON-EMPTY-EVIDENCE INVARIANT (NE-GA-RUN5, ``run_execution.py``): a gated
    Run that completed zero verified reads withholds unconditionally,
    regardless of this knob. Deployment config, NEVER
    caller/request-selectable (B-P0-1). Like ``grounding_strictness``, a
    ``config_hash`` input only when the T3 read surface is on.
    """

    grounding_gate_profile: Literal["ng1-frozen", "ng2"] = "ng2"
    """The pinned gate rulebook (design §3.2). Env
    ``NE_ONTOLOGY_GROUNDING_GATE_PROFILE``.

    ``ng1-frozen`` is byte-identical to today's gate (the ``GATE_VERSION`` string
    is preserved) and is legal ONLY with ``strict`` (the frozen ng-1 rules cannot
    classify fact-sentences — see the combo validator below). Default ``ng2``
    moves the provenance ``gate`` string to ``ng-2/<hash>`` for grounded-ON
    deployments — a visible, provenance-stamped change, never silent. Like
    ``verified_max_result_tokens`` (above), it is a ``config_hash`` input only
    when the T3 read surface is on.
    """

    # -- 015A0-AUTHORITY-T3 governed local T3 source authority ----------------

    governed_local_t3_enabled: bool = False
    """Opt-in: serve source-bearing verified answers from the governed, versioned
    local Meridian dataset as a supported source-authority producer.

    Default OFF — a default-off deployment's composition and ``config_hash`` are
    byte-identical to pre-015A0. There is deliberately NO cadence, schedule or
    connector-heartbeat knob beside this one: governed local T3 freshness is the
    atomically selected release head plus backend health and immutable-graph
    integrity, never a timer. The dataset name and every release hash come from
    the checked-in manifest, never from settings.
    """

    @field_validator("grounded_run_agent_ids", mode="before")
    @classmethod
    def _split_grounded_run_agent_ids(cls, value: object) -> object:
        """Accept the env's comma-separated form (pydantic-settings would
        otherwise demand JSON for a tuple field); blank segments are dropped."""
        if isinstance(value, str):
            return tuple(part.strip() for part in value.split(",") if part.strip())
        return value

    @model_validator(mode="after")
    def _governed_local_t3_requires_a_snapshot_backend(self) -> OntologySettings:
        """Fail loud at construction when governed local T3 is enabled on Fuseki.

        Fuseki cannot pin one immutable Store/head across a source-bearing
        invocation's primary/count/citation queries, so it is a negative capability
        rather than a second positive backend. Refusing here means a misconfigured
        deployment never boots into a state where a source-bearing read could be
        attempted — the runtime guard in
        ``ne.brain_ontology.verified.selected_release`` is the belt behind it.
        """
        if self.governed_local_t3_enabled and self.backend != "oxigraph":
            raise ValueError(
                "NE_ONTOLOGY_GOVERNED_LOCAL_T3_ENABLED=true requires "
                f"NE_ONTOLOGY_BACKEND='oxigraph': backend {self.backend!r} cannot prove the "
                "immutable selected-release snapshot a source-bearing governed local T3 read "
                "pins across its primary/count/citation queries"
            )
        return self

    @model_validator(mode="after")
    def _grounding_combo_fail_loud(self) -> OntologySettings:
        """Reject the one invalid tier x profile combo at construction (design
        D-P1-1): ``ng1-frozen`` + ``standard``. The frozen ng-1 rules emit a
        single vocab class and cannot classify fact-sentences, so ``standard``
        would tier fact-adjacent vocabulary — a silent assurance hole. Pydantic
        surfaces this as a ``ValidationError`` (a ``ValueError`` subclass), which
        IS the fail-loud boot error (raised before ``build_platform`` ever runs).
        """
        if self.grounding_gate_profile == "ng1-frozen" and self.grounding_strictness == "standard":
            raise ValueError(
                "NE_ONTOLOGY_GROUNDING_GATE_PROFILE='ng1-frozen' is valid only with "
                "NE_ONTOLOGY_GROUNDING_STRICTNESS='strict': the frozen ng-1 rules cannot "
                "classify fact-sentences, so 'standard' would tier fact-adjacent vocabulary "
                "(design D-P1-1)"
            )
        if (
            self.grounding_gate_profile == "ng1-frozen"
            and self.grounding_run_strictness == "standard"
        ):
            # Same D-P1-1 rationale on the Run knob (NE-GA-RUN4): the Run gate
            # runs the SAME profile, so ng1-frozen + a standard Run tier would
            # tier fact-adjacent vocabulary on the Run path.
            raise ValueError(
                "NE_ONTOLOGY_GROUNDING_GATE_PROFILE='ng1-frozen' is valid only with "
                "NE_ONTOLOGY_GROUNDING_RUN_STRICTNESS='strict': the frozen ng-1 rules cannot "
                "classify fact-sentences, so a 'standard' Run tier would tier fact-adjacent "
                "vocabulary (design D-P1-1)"
            )
        return self
