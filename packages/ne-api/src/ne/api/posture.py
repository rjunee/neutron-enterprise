# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The sovereignty-posture data sources for GET /v1/posture (NE-131).

Three honest, derived sources — never hand-tuned, never fabricated (§0 law 5):

* :data:`CONSTRAINT_ATTESTATIONS` — the BUILD-TIME constraint attestation table,
  not a runtime health probe. It was DERIVED from
  the project's internal readiness harness (not part of the open-source release): every
  ``enforcement`` string is that criterion's ``Criterion.title`` VERBATIM, every
  ``evidence`` tuple is the criterion's sorted ``EvaluatedCriterion.evidence``,
  and ``attested`` is ``True`` iff the harness reports the criterion PASS. A
  constraint with no mechanical guardian would render honestly ``attested=False`` /
  ``mode='manual'`` — there are currently none (every attested constraint has a
  guardian). In the open-source release this table is a STATIC snapshot of that
  evidence: the harness and its drift guard are not shipped, so a change to a
  guardian must be reflected here by hand, and ``attested`` is only as current as
  the last regeneration.

* :func:`effective_egress` — the EFFECTIVE AIR_GAP + sanctioned-egress state,
  reported from the SAME ``EgressSettings`` INSTANCE the live C2 guard enforces
  (the boot-frozen instance threaded from ``build_platform``), ORed with the
  bare-global ``ne.net.airgap.air_gap_engaged()`` which the real
  ``EgressPolicy._check_frontier`` ALSO reads per call — so both terms match
  enforcement exactly (§0 law 5 reported==enforced). It NEVER re-reads env /
  ``ne.toml`` per request. Under AIR_GAP the sanctioned frontier allowlist is EMPTY
  (the frontier lane is killed), per design §4.2 — report-only, no policy change.

* :func:`config_hash_history` — the config_hash timeline, derived from the REAL
  audit store (``ne.audit.store.ne_audit_log``): one span per distinct config_hash,
  the span in force (hash == current) left open (``to=None``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa

from ne.audit.store import ne_audit_log

if TYPE_CHECKING:
    from ne.gateway.egress import EgressSettings


@dataclass(frozen=True)
class ConstraintAttestation:
    """One C1-C12 build-time attestation, transcribed from the readiness harness.

    ``attested`` is ``True`` iff the harness reported the criterion PASS when this
    table was last regenerated (a static snapshot in the open-source release). ``mode`` is
    ``'static'`` means build-time evidence from a mechanical guardian; it never
    means the control's current runtime health. ``'manual'`` is used for a
    constraint with no mechanical guardian (there are currently none — every
    attested constraint has a guardian).
    """

    id: str
    short: str
    enforcement: str
    attested: bool
    mode: str
    evidence: tuple[str, ...]


# DERIVED from the internal readiness harness CRITERIA — see the module docstring.
# `enforcement` == Criterion.title VERBATIM; `evidence` == the sorted
# EvaluatedCriterion.evidence describe() strings; `attested` == (status is PASS);
# `mode` == 'static' for the attested entries, 'manual' for any harness-MANUAL
# constraint (currently none). Static snapshot in the open-source release.
CONSTRAINT_ATTESTATIONS: tuple[ConstraintAttestation, ...] = (
    ConstraintAttestation(
        id="C1",
        short="SELF-HOSTED",
        enforcement="No egress: sealed-stack air-gap proof",
        attested=True,
        mode="static",
        evidence=(
            ".github/workflows/no-egress.yml",
            ".github/workflows/no-egress.yml :: test_static_harness.py",
            "deploy/airgap/scripts/netns-seal.sh",
            "deploy/airgap/tests/test_static_harness.py",
        ),
    ),
    ConstraintAttestation(
        id="C2",
        short="SEALED EGRESS",
        enforcement="No egress: per-package egress guardians",
        attested=True,
        mode="static",
        evidence=(
            ".github/workflows/no-egress.yml :: test_airgap_phase_c.py",
            "packages/ne-brain/tests/test_c2_egress_guardian.py",
            "packages/ne-chat/tests/test_c2_egress_guardian.py",
            "packages/ne-connect/tests/test_c2_egress_guardian.py",
            "packages/ne-gateway/tests/test_egress_policy.py",
            "packages/ne-gateway/tests/test_no_egress.py",
            "packages/ne-gateway/tests/test_ssrf.py",
            "packages/ne-serve/tests/test_c2_egress_guardian.py",
            "tools/ne-backup/tests/test_c2_egress_guardian.py",
        ),
    ),
    ConstraintAttestation(
        id="C3",
        short="MODEL-INDEPENDENT",
        enforcement="Import boundaries: lint-imports contracts enforced in CI",
        attested=True,
        mode="static",
        evidence=(
            ".github/workflows/ci.yml :: lint-imports",
            ".importlinter",
        ),
    ),
    ConstraintAttestation(
        id="C4",
        short="LICENSE GATE",
        enforcement="License gate (Python + npm) enforced in supply-chain CI",
        attested=True,
        mode="static",
        evidence=(
            ".github/workflows/supply-chain.yml :: ne check licenses",
            ".github/workflows/supply-chain.yml :: npm_license_gate.py",
            "tools/ne-cli/tests/test_license_gate.py",
            "tools/supply-chain/license-policy.toml",
        ),
    ),
    ConstraintAttestation(
        id="C5",
        short="SWAPPABLE SLOTS",
        enforcement="Swap-parity: dual-backend + Cedar/Rego conformance",
        attested=True,
        mode="static",
        evidence=(
            "packages/ne-brain/tests/test_swap_parity_qdrant_pgvector.py",
            "packages/ne-contracts/src/ne/contracts/testing",
            "packages/ne-govern/tests/test_cedar_parity.py",
            "packages/ne-memory-graph/tests/test_dual_backend_conformance.py",
        ),
    ),
    ConstraintAttestation(
        id="C6",
        short="PDP CHOKEPOINT",
        enforcement="Governed executor: no-bypass + PDP audit coupling",
        attested=True,
        mode="static",
        evidence=(
            ".github/workflows/ci.yml :: test_rego_policies.py",
            "packages/ne-govern/tests/test_pdp_audit_coupling.py",
            "packages/ne-govern/tests/test_pdp_no_bypass.py",
            "packages/ne-govern/tests/test_rego_policies.py",
            "packages/ne-runtime/tests/test_c6_no_bypass_guardian.py",
        ),
    ),
    ConstraintAttestation(
        id="C7",
        short="TRIMMED KNOWLEDGE",
        enforcement="Provable ACL trimming: vector + graph retrievers",
        attested=True,
        mode="static",
        evidence=(
            "packages/ne-brain-graph/tests/test_c7_graph_provable_trimming.py",
            "packages/ne-brain-graph/tests/test_c7_graph_trimming_guardian.py",
            "packages/ne-brain/tests/test_c7_provable_trimming.py",
            "packages/ne-brain/tests/test_c7_trimming_guardian.py",
        ),
    ),
    ConstraintAttestation(
        id="C8",
        short="KPI-FIRST",
        enforcement="KPI tagging: event schema + observability models",
        attested=True,
        mode="static",
        evidence=(
            "packages/ne-obs/tests/test_kpi_models.py",
            "packages/ne-obs/tests/test_report_generator.py",
            "packages/ne-schema/src/ne/schema/schemas/events.yaml :: kpi_tag",
        ),
    ),
    ConstraintAttestation(
        id="C9",
        short="DEPLOY ANYWHERE",
        enforcement="Deploy hygiene: Helm + Terraform + EKS hardened-reference lanes",
        attested=True,
        mode="static",
        evidence=(
            ".github/workflows/helm-smoke.yml",
            ".github/workflows/terraform-validate.yml",
            "tools/ne-cli/tests/test_helm_chart.py",
            "tools/ne-cli/tests/test_terraform_hygiene.py",
        ),
    ),
    ConstraintAttestation(
        id="C10",
        short="AUDIT INTEGRITY",
        enforcement="Tamper-evident audit: append-only + hash-chained + verifiable",
        attested=True,
        mode="static",
        evidence=(
            "packages/ne-audit/tests/test_append_only.py",
            "packages/ne-audit/tests/test_chain_verify.py",
            "packages/ne-audit/tests/test_checkpoint.py",
            "packages/ne-audit/tests/test_column_tamper.py",
            "packages/ne-audit/tests/test_redteam_tamper.py",
            "packages/ne-worker/tests/test_audit_checkpoint_loop.py",
            "tools/ne-cli/tests/test_audit_cli.py",
        ),
    ),
    ConstraintAttestation(
        id="C11",
        short="SUPPLY CHAIN",
        enforcement="Supply-chain integrity: lock/pins/provenance/signing/SBOM",
        attested=True,
        mode="static",
        evidence=(
            ".github/workflows/supply-chain.yml :: ne check lock",
            "tools/ne-cli/tests/test_lock_policy.py",
            "tools/ne-cli/tests/test_provenance.py",
            "tools/ne-cli/tests/test_sbom.py",
            "tools/ne-cli/tests/test_signing.py",
            "tools/ne-cli/tests/test_workflow_pins.py",
        ),
    ),
    ConstraintAttestation(
        id="C12",
        short="INJECTION-ASSUMED",
        enforcement="Abuse defense: red-team + rate-limit + recipient allowlist",
        attested=True,
        mode="static",
        evidence=(
            "packages/ne-govern/tests/test_rate_limit.py",
            "packages/ne-govern/tests/test_recipient_allowlist.py",
            "packages/ne-govern/tests/test_redteam_c12.py",
            "tools/ne-cli/tests/test_pilot_secret_hygiene.py",
        ),
    ),
)

#: The provenance string the /v1/posture payload carries so a reader knows exactly
#: where the attestation comes from and how it is kept honest.
ATTESTATION_SOURCE = (
    "build-time guardian evidence (static snapshot; the generating readiness "
    "harness is not part of this release)"
)


def _to_utc(value: datetime) -> datetime:
    """Normalize a possibly-naive datetime to tz-aware UTC (SQLite reads naive).

    Copied from ``ne.api.wire._to_utc`` — the audit store's ``occurred_at`` column
    is ``DateTime(timezone=True)`` but SQLite returns naive datetimes on read.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def effective_egress(settings: EgressSettings) -> tuple[bool, list[str]]:
    """Return the EFFECTIVE ``(air_gap, sanctioned_frontier_allowlist)``.

    Reports the BOOT-FROZEN ``EgressSettings`` instance the live C2 guard enforces
    (threaded from ``build_platform`` — the SAME instance ``build_egress_guard`` was
    built over), ORing its ``.air_gap`` with the bare-global
    ``ne.net.airgap.air_gap_engaged()`` — the per-call flag the real
    ``EgressPolicy._check_frontier`` ALSO consults, so BOTH terms match enforcement
    exactly (§0 law 5 reported==enforced). It NEVER re-reads env / ``ne.toml`` per
    request. Under AIR_GAP the frontier lane is killed, so the EFFECTIVE sanctioned
    allowlist is EMPTY (design §4.2); otherwise it is the sorted configured frontier
    hosts. Report-only — this composes no policy.
    """
    # LOCAL import: keep ne-net off ne-api's eager import graph edges here, mirroring
    # the module-level lazy-import discipline in platform.py.
    from ne.net.airgap import air_gap_engaged

    air_gap = bool(settings.air_gap or air_gap_engaged())
    if air_gap:
        return True, []
    return False, sorted(settings.allowed_frontier_hosts)


def config_hash_history(
    engine: sa.Engine, current_hash: str
) -> list[tuple[str, datetime, datetime | None]]:
    """Derive the config_hash timeline from the REAL audit store.

    One span per distinct ``config_hash`` in ``ne_audit_log``: ``(hash, from, to)``
    where ``from`` is the earliest ``occurred_at`` the hash was stamped on and
    ``to`` is the latest — EXCEPT the span whose hash equals ``current_hash`` (the
    hash in force), which is left open (``to=None``). Ordered oldest-first by the
    hash's first chain position (``MIN(seq)``). An empty log yields ``[]``.
    """
    stmt = (
        sa.select(
            ne_audit_log.c.config_hash,
            sa.func.min(ne_audit_log.c.occurred_at),
            sa.func.max(ne_audit_log.c.occurred_at),
            sa.func.min(ne_audit_log.c.seq),
        )
        .group_by(ne_audit_log.c.config_hash)
        .order_by(sa.func.min(ne_audit_log.c.seq))
    )
    spans: list[tuple[str, datetime, datetime | None]] = []
    with engine.connect() as conn:
        for config_hash, first_at, last_at, _min_seq in conn.execute(stmt):
            to: datetime | None = None if config_hash == current_hash else _to_utc(last_at)
            spans.append((config_hash, _to_utc(first_at), to))
    return spans
