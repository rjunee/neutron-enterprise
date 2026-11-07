# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Static validation of the deploy/compose/dev stack (NE-006).

No live docker daemon is required or used here: the compose YAML is parsed
with PyYAML and asserted against the SPEC §5 NE-006 service list. This is
the CI-safe alternative to actually bringing up the stack.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
DEV_DIR = REPO_ROOT / "deploy" / "compose" / "dev"
COMPOSE_FILE = DEV_DIR / "compose.yaml"

# The SPEC §5 NE-006 service list (TEI split into embed+rerank per the §4.1
# infra row) — services that must always run, with no compose profile.
BASE_SERVICES = frozenset(
    {
        "postgres",
        "qdrant",
        "keycloak",
        "opa",
        "tei-embed",
        "tei-rerank",
        "openbao",
        "otel-collector",
    }
)

# Langfuse v3 + its ops subsystem — optional per SPEC §3.3, gated behind the
# "langfuse" compose profile.
LANGFUSE_SERVICES = frozenset(
    {
        "langfuse-web",
        "langfuse-worker",
        "clickhouse",
        "valkey",
        "seaweedfs",
        "langfuse-s3-init",
    }
)

# NE-087 (O-4): OpenFGA fine-grained-sharing sidecar + one-shot migrate, gated
# behind the "rebac" compose profile (started with `ne dev up --rebac`).
REBAC_SERVICES = frozenset({"openfga", "openfga-migrate"})

# NE-S3-OBJECTSTORE: the MinIO Object Lock server backing the WORM proof tests.
# CI/dev test infrastructure only — never shipped, never a product dependency.
WORM_SERVICES = frozenset({"minio"})

EXPECTED_IMAGES: dict[str, str] = {
    "postgres": "postgres:18.6-alpine",
    "qdrant": "qdrant/qdrant:v1.18.2",
    "keycloak": "quay.io/keycloak/keycloak:26.7.4",
    "opa": "openpolicyagent/opa:1.18.2-debug",
    "tei-embed": "ghcr.io/huggingface/text-embeddings-inference:cpu-1.9.3",
    "tei-rerank": "ghcr.io/huggingface/text-embeddings-inference:cpu-1.9.3",
    "openbao": "openbao/openbao:2.6.3",
    "otel-collector": "otel/opentelemetry-collector-contrib:0.155.0",
    "langfuse-web": "langfuse/langfuse:3.205.0",
    "langfuse-worker": "langfuse/langfuse-worker:3.205.0",
    "clickhouse": "clickhouse/clickhouse-server:25.8.26.11-alpine",
    "valkey": "valkey/valkey:8.1.10-alpine",
    "seaweedfs": "chrislusf/seaweedfs:4.37",
    "langfuse-s3-init": "chrislusf/seaweedfs:4.37",
    # NE-087 (O-4): dev tier pins tags exactly (not digests).
    "openfga": "openfga/openfga:v1.18.3",
    "openfga-migrate": "openfga/openfga:v1.18.3",
    # NE-S3-OBJECTSTORE: MinIO is digest-pinned, not tag-pinned — the WORM proof
    # is only meaningful against a known-good Object Lock implementation, so the
    # exact image is asserted here and read back out of this same compose file by
    # the CI WORM job (.github/workflows/ci.yml) rather than restated there.
    "minio": (
        "coollabsio/minio@sha256:69b55a1c1c5dc285ce04db96689f5b2102317fc77a50680a1874ca6efd1c87f9"
    ),
}


def _load_compose() -> dict:
    with COMPOSE_FILE.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def test_compose_file_parses() -> None:
    data = _load_compose()
    assert "services" in data
    assert isinstance(data["services"], dict)


def test_base_services_match_spec() -> None:
    data = _load_compose()
    services = data["services"]
    base = {name for name, cfg in services.items() if "profiles" not in cfg}
    assert base == BASE_SERVICES


def test_langfuse_services_behind_profile() -> None:
    data = _load_compose()
    services = data["services"]
    # Profile-set assertions: langfuse services → [langfuse], rebac services →
    # [rebac], everything else profile-less (the SPEC §5 base tier).
    for name in LANGFUSE_SERVICES:
        assert services[name].get("profiles") == ["langfuse"], name
    for name in REBAC_SERVICES:
        assert services[name].get("profiles") == ["rebac"], name
    for name in WORM_SERVICES:
        assert services[name].get("profiles") == ["worm"], name
    for name, cfg in services.items():
        if name not in LANGFUSE_SERVICES | REBAC_SERVICES | WORM_SERVICES:
            assert "profiles" not in cfg, name


def test_rebac_services_behind_profile() -> None:
    data = _load_compose()
    services = data["services"]
    for name in REBAC_SERVICES:
        assert services[name].get("profiles") == ["rebac"], name
    # The one-shot migrate does not restart; openfga has a grpc_health_probe check.
    assert services["openfga-migrate"].get("restart") == "no"
    assert "grpc_health_probe" in " ".join(services["openfga"]["healthcheck"]["test"])
    assert services["openfga"]["ports"] == ["8087:8080"]


def test_images_pinned_exactly() -> None:
    data = _load_compose()
    services = data["services"]
    assert (
        set(EXPECTED_IMAGES) == BASE_SERVICES | LANGFUSE_SERVICES | REBAC_SERVICES | WORM_SERVICES
    )
    for name, expected_image in EXPECTED_IMAGES.items():
        image = services[name]["image"]
        assert image == expected_image, name
        assert not image.endswith(":latest"), name
        assert ":" in image, name


def test_base_services_have_healthchecks() -> None:
    data = _load_compose()
    services = data["services"]
    for name in BASE_SERVICES - {"otel-collector"}:
        assert "healthcheck" in services[name], name


def test_telemetry_disabled() -> None:
    data = _load_compose()
    services = data["services"]

    qdrant_env = services["qdrant"]["environment"]
    assert qdrant_env.get("QDRANT__TELEMETRY_DISABLED") == "true"

    opa_command = services["opa"]["command"]
    assert "--disable-telemetry" in opa_command

    for name in ("langfuse-web", "langfuse-worker"):
        assert services[name]["environment"].get("TELEMETRY_ENABLED") == "false"

    for name in ("tei-embed", "tei-rerank"):
        assert services[name]["environment"].get("HF_HUB_DISABLE_TELEMETRY") == "1"


def test_seed_script_exists_and_executable() -> None:
    script = DEV_DIR / "seed" / "seed.sh"
    assert script.is_file()
    assert os.access(script, os.X_OK)


def test_initdb_creates_required_databases() -> None:
    sql = (DEV_DIR / "initdb" / "01-databases.sql").read_text(encoding="utf-8")
    assert "CREATE DATABASE keycloak" in sql
    assert "CREATE DATABASE langfuse" in sql
    assert "CREATE DATABASE openfga" in sql


def test_otel_config_parses_with_otlp_receiver() -> None:
    otel_path = DEV_DIR / "otel" / "otel-collector-config.yaml"
    with otel_path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    assert "otlp" in data["receivers"]

    pipelines = data["service"]["pipelines"]
    assert pipelines
    for pipeline in pipelines.values():
        assert pipeline["receivers"][0] == "otlp"
