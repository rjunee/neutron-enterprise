# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Static guardian for the deploy/compose/pilot tier (NE-070).

No docker daemon needed: the compose YAML is parsed with PyYAML and asserted
against the NE-070 hardening contract (profile matrix, C11 digest pins, TLS/
loopback topology, least-privilege, C1/C2 telemetry-off). This is the CI-safe
ratchet — the live bring-up is exercised separately.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
PILOT_DIR = REPO_ROOT / "deploy" / "compose" / "pilot"
COMPOSE_FILE = PILOT_DIR / "compose.yaml"
COMPOSE_CI = REPO_ROOT / "deploy" / "airgap" / "scripts" / "compose-ci.sh"

# NE-103b: the pilot Keycloak realm + the two Caddyfiles (self-signed + customcerts).
REALM_FILE = PILOT_DIR / "keycloak" / "ne-pilot-realm.json"
CADDYFILES = (PILOT_DIR / "caddy" / "Caddyfile", PILOT_DIR / "caddy" / "Caddyfile.customcerts")


def _realm() -> dict:
    return json.loads(REALM_FILE.read_text(encoding="utf-8"))


# The decided service→profile matrix (pilot-DEFINED services only; the serve
# gpu/cpu services ride in via `include` and are NOT redefined here).
PROFILE_MATRIX: dict[str, set[str]] = {
    # NE-103c: the durable ne-worker rides the core profile (SPEC §5 row overrides
    # design §8's reserved 'agents' — ne-api enqueues so its executor co-starts).
    "core": {
        "caddy",
        "postgres",
        "age-admission",
        "keycloak",
        "opa",
        "ne-api",
        "ne-worker",
    },
    "brain": {"qdrant", "tei-embed", "tei-rerank"},
    "obs": {"otel-collector"},
    # NE-087 (O-4): OpenFGA fine-grained-sharing sidecar + one-shot migrate, OFF by
    # default — enabled only with `ne up --profile rebac`.
    "rebac": {"openfga", "openfga-migrate"},
}
ALL_PILOT_SERVICES = set().union(*PROFILE_MATRIX.values())

# The one service allowed to publish off-box (TLS ingress).
OFF_BOX_SERVICE = "caddy"
# NE-087 (O-4): the one-shot init job(s) — run to completion, restart "no", no
# healthcheck. Pinned by a SET-EQUATION in test_every_service_hardened so no other
# service can quietly adopt the restart-exemption.
ONE_SHOT_INIT_SERVICES = {"age-admission", "openfga-migrate"}
# otel-collector is distroless (no shell); openfga-migrate is a one-shot init job —
# the documented healthcheck exemptions.
HEALTHCHECK_EXEMPT = {"age-admission", "otel-collector", "openfga-migrate"}

# NE-087 (O-4): the pinned OpenFGA image (patched latest; see README provenance).
REBAC_IMAGE = (
    "openfga/openfga:v1.18.3@sha256:"
    "01a6000aa6040a4d0bde6ea1d3359ac3b9f21dc972ac6103a87b38659a836776"
)
# The five NE_REBAC_* app-env pass-through keys (null in compose; FIX-1).
REBAC_APP_ENV_KEYS = {
    "NE_REBAC_ENABLED",
    "NE_REBAC_API_URL",
    "NE_REBAC_STORE_ID",
    "NE_REBAC_AUTHORIZATION_MODEL_ID",
    "NE_REBAC_API_TOKEN",
}

# NE-103a build-in-seal + local-digest pin (owner dec ii): the first-party
# `ne-app` image is built locally during the air-gap staging window and has NO
# published registry digest, so the strict C11 tag@sha256 rule cannot apply to
# it. These services carry the NARROW, negatively-tested first-party exception
# (semver local tag `ne-app:X.Y.Z` + `pull_policy: never` + no registry prefix +
# no @digest); NO third-party image may adopt it. The real pin is the staging
# --iidfile image id asserted against the running container in air-gap Phase A.
FIRST_PARTY_BUILD_SERVICES = {"ne-api", "ne-worker"}
_FIRST_PARTY_IMAGE = re.compile(r"^ne-app:\d+\.\d+\.\d+$")
FIRST_PARTY_EXACT_ID_SERVICES = {"postgres", "age-admission"}
_FIRST_PARTY_EXACT_ID = re.compile(r"^sha256:[0-9a-f]{64}$")

_DIGEST_PIN = re.compile(r"^[^@\s]+:[^@\s]+@sha256:[0-9a-f]{64}$")

# --- app-root RAM bounds (the 2026-09-05 memcg OOM class) --------------------
# The two app roots share one image, one memory cap and one /tmp bound. Both
# numbers are asserted, not merely read, because a tmpfs page is charged to the
# container's MEMORY cgroup: an unbounded `- /tmp` lets a runaway write consume
# the service's whole memory limit as `shmem` while `df` inside the container
# still reports tens of free gigabytes.
APP_ROOTS = ("ne-api", "ne-worker")
APP_TMPFS_SIZE = "512m"
APP_MEMORY_LIMIT = "2g"
# ne-api's measured resting/peak anon (MiB) while serving only health polls, from
# the /proc/<pid>/smaps profile that motivated MALLOC_ARENA_MAX. The tmpfs bound
# must leave room for it INSIDE the memory cap, or the cap never fires as ENOSPC.
NE_API_PEAK_ANON_MIB = 1145
_BYTE_UNITS = {"b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


def _byte_count(value: str) -> int:
    """Parse a Compose byte quantity (`512m`, `2g`) the way the engine does."""
    match = re.fullmatch(r"(\d+)([bkmg])?", value.lower())
    assert match is not None, f"unparseable byte quantity {value!r}"
    return int(match.group(1)) * _BYTE_UNITS[match.group(2) or "b"]


def _load() -> dict:
    with COMPOSE_FILE.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _services() -> dict[str, dict]:
    return _load()["services"]


def test_compose_parses_and_names_pilot() -> None:
    data = _load()
    assert data["name"] == "ne-pilot"
    assert isinstance(data["services"], dict)


def test_agent_registry_rollout_labels_are_exact_and_only_on_api_worker() -> None:
    services = _services()
    label = "neutron.dev/agent-registry-sha256"
    expected = "${NE_AGENT_REGISTRY_RAW_SHA256:?set by governed Compose executor}"
    labeled = {
        name: service.get("labels", {}).get(label)
        for name, service in services.items()
        if label in service.get("labels", {})
    }
    assert labeled == {"ne-api": expected, "ne-worker": expected}


def test_skill_bundle_is_one_exact_read_only_source_with_shared_readiness() -> None:
    data = _load()
    services = _services()
    expected_source = "../../skills"
    expected_target = "/etc/neutron/skills"
    expected_hash_env = "${NE_SKILL_BUNDLE_RAW_SHA256:?set by governed Compose executor}"
    assert data["x-ne-app-env"]["NE_SKILLS_BUNDLE_DIR"] == expected_target
    assert data["x-ne-app-env"]["NE_SKILL_BUNDLE_RAW_SHA256"] == expected_hash_env
    for service_name in ("ne-api", "ne-worker"):
        volumes = services[service_name]["volumes"]
        assert f"{expected_source}:{expected_target}:ro" in volumes
        assert (
            services[service_name]["labels"]["neutron.dev/skill-bundle-sha256"] == expected_hash_env
        )
    assert (
        PILOT_DIR / expected_source / "meridian-hardship-draft" / "SKILL.md"
    ).resolve().read_bytes() == (
        REPO_ROOT / "deploy/skills/meridian-hardship-draft/SKILL.md"
    ).read_bytes()
    api_probe = " ".join(services["ne-api"]["healthcheck"]["test"])
    assert "http://127.0.0.1:8000/healthz" in api_probe
    assert "ne.api" not in api_probe
    assert "skills_delivery" not in api_probe
    assert services["ne-api"]["depends_on"]["ne-worker"] == {"condition": "service_healthy"}


def test_airgap_compose_executor_overwrites_ambient_with_exact_registry_and_skill_hashes(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    observed = tmp_path / "observed"
    docker = fake_bin / "docker"
    docker.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'printf \'%s\\n\' "$NE_AGENT_REGISTRY_RAW_SHA256" >"$OBSERVED"\n'
        'printf \'%s\\n\' "$NE_SKILL_BUNDLE_RAW_SHA256" >>"$OBSERVED"\n'
        'printf \'%s\\n\' "$*" >>"$OBSERVED"\n',
        encoding="utf-8",
    )
    docker.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "OBSERVED": str(observed),
        "NE_AGENT_REGISTRY_RAW_SHA256": "0" * 64,
        "NE_SKILL_BUNDLE_RAW_SHA256": "0" * 64,
    }

    result = subprocess.run(
        ["bash", str(COMPOSE_CI), "config"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    lines = observed.read_text(encoding="utf-8").splitlines()
    assert (
        lines[0]
        == hashlib.sha256((REPO_ROOT / "deploy/agents/registry.toml").read_bytes()).hexdigest()
    )
    assert (
        lines[1]
        == hashlib.sha256(
            (REPO_ROOT / "deploy/skills/meridian-hardship-draft/SKILL.md").read_bytes()
        ).hexdigest()
    )
    assert lines[2].startswith("compose --project-name ne-pilot ")
    assert "deploy/compose/pilot/compose.yaml" in lines[2]


def test_agent_registry_is_one_read_only_source_for_api_and_worker() -> None:
    data = _load()
    expected_source = "../../agents/registry.toml"
    expected_target = "/etc/neutron/agents/registry.toml"
    assert data["x-ne-app-env"]["NE_API_AGENT_MANIFEST_PATH"] == expected_target
    for service_name in ("ne-api", "ne-worker"):
        volumes = _services()[service_name]["volumes"]
        assert volumes == [
            f"{expected_source}:{expected_target}:ro",
            "../../skills:/etc/neutron/skills:ro",
        ]
    assert (PILOT_DIR / expected_source).resolve().read_bytes() == (
        REPO_ROOT / "deploy/agents/registry.toml"
    ).read_bytes()


def test_service_profile_matrix_exact() -> None:
    services = _services()
    assert set(services) == ALL_PILOT_SERVICES, "unexpected/missing pilot service"
    for profile, expected in PROFILE_MATRIX.items():
        for name in expected:
            assert services[name].get("profiles") == [profile], name
    # Every pilot service is behind exactly one of the three profiles.
    for name, cfg in services.items():
        assert cfg.get("profiles"), f"{name} has no profile"
        assert len(cfg["profiles"]) == 1, name


def test_every_image_is_digest_pinned() -> None:
    for name, cfg in _services().items():
        image = cfg["image"]
        if name in FIRST_PARTY_BUILD_SERVICES:
            # First-party build-in-seal exception (owner dec ii): NARROW and
            # explicit — semver local tag, pull_policy never, no registry prefix,
            # no @digest. NOT the strict tag@sha256 rule; the pin is the Phase-A
            # --iidfile image-id assert.
            assert _FIRST_PARTY_IMAGE.match(image), f"{name} first-party image malformed: {image}"
            assert cfg.get("pull_policy") == "never", (
                f"{name} first-party image must set pull_policy: never (block "
                f"registry squatting + skip `compose pull`)"
            )
            assert "/" not in image, f"{name} first-party image must have no registry prefix"
            assert "@" not in image, f"{name} first-party image must carry no @digest"
            assert not image.endswith(":latest"), name
            continue
        if name in FIRST_PARTY_EXACT_ID_SERVICES:
            assert _FIRST_PARTY_EXACT_ID.fullmatch(image), name
            assert cfg.get("pull_policy") == "never", name
            continue
        assert _DIGEST_PIN.match(image), f"{name} image not tag@sha256 pinned: {image}"
        assert not image.endswith(":latest"), name


def test_first_party_exception_is_narrow() -> None:
    """The build-in-seal exception must be exactly {ne-api}: the set of services
    whose image is NOT strict-digest-pinned == FIRST_PARTY_BUILD_SERVICES, AND the
    set of services carrying pull_policy == FIRST_PARTY_BUILD_SERVICES (no
    third-party service may adopt the exception)."""
    services = _services()
    not_digest_pinned = {
        name for name, cfg in services.items() if not _DIGEST_PIN.match(cfg["image"])
    }
    expected_local = FIRST_PARTY_BUILD_SERVICES | FIRST_PARTY_EXACT_ID_SERVICES
    assert not_digest_pinned == expected_local, (
        f"first-party exception drifted: {not_digest_pinned} != {expected_local}"
    )
    carries_pull_policy = {name for name, cfg in services.items() if "pull_policy" in cfg}
    assert carries_pull_policy == expected_local, (
        f"pull_policy must appear on exactly the local first-party services: {carries_pull_policy}"
    )


def test_first_party_tag_single_source() -> None:
    """The ne-app image tag is single-sourced across compose, the build script,
    and up.py — drift in any one is a guardian failure."""
    from ne.cli.up import NE_APP_IMAGE

    compose_image = _services()["ne-api"]["image"]
    build_script = (REPO_ROOT / "deploy" / "airgap" / "scripts" / "build-ne-app.sh").read_text(
        encoding="utf-8"
    )
    match = re.search(r'IMAGE_TAG="([^"]+)"', build_script)
    assert match, "build-ne-app.sh must define IMAGE_TAG"
    assert compose_image == match.group(1) == NE_APP_IMAGE, (
        f"ne-app tag drift: compose={compose_image!r} "
        f"build-ne-app.sh={match.group(1)!r} up.py={NE_APP_IMAGE!r}"
    )


def test_ne_api_publishes_no_ports() -> None:
    # ne-api is reached only via Caddy (103b); it must publish nothing off-box,
    # keeping caddy the sole off-box publisher (test_caddy_is_the_only_off_box).
    assert "ports" not in _services()["ne-api"]


def test_ne_api_depends_on_core_healthy() -> None:
    depends = _services()["ne-api"]["depends_on"]
    assert set(depends) == {"age-admission", "postgres", "keycloak", "opa", "ne-worker"}
    assert depends["age-admission"] == {"condition": "service_completed_successfully"}
    for dep in ("postgres", "keycloak", "opa", "ne-worker"):
        assert depends[dep] == {"condition": "service_healthy"}, dep


def test_ne_api_read_only_rootfs() -> None:
    cfg = _services()["ne-api"]
    assert cfg.get("read_only") is True, "ne-api rootfs must be read-only"
    assert cfg.get("tmpfs") == [f"/tmp:size={APP_TMPFS_SIZE}"], (
        "ne-api needs a writable tmpfs /tmp, and it must be SIZE-BOUNDED"
    )


def test_ne_api_env_wiring() -> None:
    env = _services()["ne-api"]["environment"]
    required = {
        "NE_API_BIND_HOST",
        "NE_API_QDRANT_URL",
        "NE_API_QDRANT_COLLECTION",
        "NE_API_TEI_URL",
        "NE_GOVERN_OPA_URL",
        "NE_IDENTITY_OIDC__ISSUER",
        "NE_IDENTITY_OIDC__AUDIENCE",
        "NE_API_APPROVER_ROLES",
        "NE_API_OPS_ROLES",
        "NE_API_STEWARD_ROLES",
        "NE_RUNTIME_SESSION_DATABASE_URL",
        "NE_AUDIT_DATABASE_URL",
        # ne-permissions: PermissionsSettings now overrides env_prefix (NE-CLEAN4),
        # so it reads NE_PERMISSIONS_DATABASE_URL consistently with every sibling.
        "NE_PERMISSIONS_DATABASE_URL",
        "NE_FLYWHEEL_SIGNAL_DATABASE_URL",
        "NE_API_EVAL_DATABASE_URL",
        # NE-103b: console static serving + OIDC-live JWKS wiring.
        "NE_API_SERVE_CONSOLE_DIST",
        "NE_IDENTITY_OIDC__JWKS_URI",
        "NE_IDENTITY_OIDC__ALLOW_INSECURE_HTTP",
    }
    assert required <= set(env), f"missing ne-api env keys: {required - set(env)}"
    for key, value in env.items():
        if key.endswith("_DATABASE_URL"):
            assert "${NE_PILOT_POSTGRES_PASSWORD" in value, f"{key} must interpolate the pw var"
            # No literal password: the only credential material is the ${...} var.
            assert ":@" not in value and "password" not in value.lower().replace(
                "${ne_pilot_postgres_password}", ""
            ), f"{key} appears to carry a literal password: {value!r}"


def test_caddy_is_the_only_off_box_publisher() -> None:
    for name, cfg in _services().items():
        for port in cfg.get("ports", []):
            spec = port if isinstance(port, str) else str(port)
            off_box = not spec.startswith("127.0.0.1:")
            if off_box:
                assert name == OFF_BOX_SERVICE, (
                    f"{name} publishes a non-loopback port {spec!r}; only "
                    f"{OFF_BOX_SERVICE} may publish off-box (TLS ingress)"
                )
    # And caddy DOES publish 443 + 8443 (the TLS ports).
    caddy_ports = {p.split(":")[0] for p in _services()["caddy"]["ports"]}
    assert {"443", "8443"} <= caddy_ports


def test_every_service_hardened() -> None:
    services = _services()
    # SET-EQUATION carve-out (NE-087): the set of services NOT restart:unless-stopped
    # is EXACTLY the one-shot init jobs — no third-party service may adopt the
    # restart-exemption without tripping this.
    non_persistent = {
        name for name, cfg in services.items() if cfg.get("restart") != "unless-stopped"
    }
    assert non_persistent == ONE_SHOT_INIT_SERVICES, (
        f"restart-exemption drifted: {non_persistent} != {ONE_SHOT_INIT_SERVICES}"
    )
    for name, cfg in services.items():
        assert cfg.get("security_opt") == ["no-new-privileges:true"], name
        if name in ONE_SHOT_INIT_SERVICES:
            assert cfg.get("restart") == "no", f"{name} one-shot must set restart: 'no'"
        else:
            assert cfg.get("restart") == "unless-stopped", name
        limits = cfg.get("deploy", {}).get("resources", {}).get("limits", {})
        assert "memory" in limits and "cpus" in limits, f"{name} missing resource limits"
        if name not in HEALTHCHECK_EXEMPT:
            assert "healthcheck" in cfg, f"{name} missing healthcheck"


def test_stateless_sidecars_drop_all_caps() -> None:
    services = _services()
    # ne-api is a stateless first-party sidecar (all state in Postgres) — it drops
    # ALL caps and adds none (publishes nothing, binds a non-privileged high port).
    # openfga + its one-shot migrate are stateless sidecars too (state in Postgres).
    for name in (
        "caddy",
        "opa",
        "otel-collector",
        "ne-api",
        "ne-worker",
        "openfga",
        "openfga-migrate",
    ):
        assert services[name].get("cap_drop") == ["ALL"], name
    # Caddy needs exactly NET_BIND_SERVICE back (binds 443/8443), nothing more.
    assert services["caddy"].get("cap_add") == ["NET_BIND_SERVICE"]
    assert "cap_add" not in services["opa"]
    assert "cap_add" not in services["otel-collector"]
    assert "cap_add" not in services["ne-api"]
    assert "cap_add" not in services["ne-worker"]
    assert "cap_add" not in services["openfga"]
    assert "cap_add" not in services["openfga-migrate"]


# --- NE-103c: the durable ne-worker service --------------------------------


def test_ne_worker_is_one_image_diff_command() -> None:
    """ne-worker is the SAME ne-app image as ne-api, launched with a worker command."""
    services = _services()
    assert services["ne-worker"]["image"] == services["ne-api"]["image"]
    assert services["ne-worker"]["command"] == ["/app/.venv/bin/python", "-m", "ne.worker"]
    # No inbound HTTP surface — the worker publishes nothing off-box.
    assert "ports" not in services["ne-worker"]


def test_ne_worker_hardened_and_scoped() -> None:
    cfg = _services()["ne-worker"]
    assert cfg.get("read_only") is True
    assert cfg.get("tmpfs") == [f"/tmp:size={APP_TMPFS_SIZE}"]
    # AGE admission must finish before this DBOS/graph-capable consumer starts.
    assert set(cfg["depends_on"]) == {"age-admission", "postgres", "opa"}
    assert cfg["depends_on"]["age-admission"] == {"condition": "service_completed_successfully"}
    for dep in ("postgres", "opa"):
        assert cfg["depends_on"][dep] == {"condition": "service_healthy"}, dep
    # The --health heartbeat-marker healthcheck (never a full re-import per probe).
    assert cfg["healthcheck"]["test"] == [
        "CMD",
        "/app/.venv/bin/python",
        "-m",
        "ne.worker",
        "--health",
    ]


def test_ne_worker_shares_config_hash_env_with_ne_api() -> None:
    """config_hash parity: the worker sees IDENTICAL frozen settings to ne-api.

    Any env value differing between the two processes forks the composed hash and
    silently splits audit correlation (design §6.2/Q5), so the worker's env MUST be a
    superset of ne-api's with identical shared values (+ its NE_WORKER_* knobs).
    """
    services = _services()
    api_env = services["ne-api"]["environment"]
    worker_env = services["ne-worker"]["environment"]
    assert set(api_env) <= set(worker_env), set(api_env) - set(worker_env)
    for key, value in api_env.items():
        assert worker_env[key] == value, f"{key} diverges between ne-api and ne-worker"
    assert "NE_WORKER_DBOS_DATABASE_URL" in worker_env
    # Both processes run durability=dbos and enqueue mode.
    assert worker_env["NE_RUNTIME_DURABILITY"] == "dbos"
    assert worker_env["NE_API_TURN_EXECUTION"] == "enqueue"


# --- app-root RAM bounds: the tmpfs cap + the glibc arena cap ----------------


def test_app_tmpfs_is_size_bounded() -> None:
    """Both app roots' `/tmp` carries an explicit `size=`, identical across roots.

    An unbounded `tmpfs: - /tmp` defaults to half of HOST RAM (~31 GB was what
    `df` reported inside the container) while every page written is charged to
    the container's 2 GiB memory cgroup. That is the 2026-09-05 kill: the kernel
    dump recorded `constraint=CONSTRAINT_MEMCG`, `usage 2097168kB limit
    2097152kB`, `shmem 1780142080` against `anon 306941952` — 1.78 GB of the cap
    was tmpfs and only 0.31 GB was process memory. The bound converts that OOM
    crash-loop into one ENOSPC on the offending write.
    """
    services = _services()
    for name in APP_ROOTS:
        rows = services[name].get("tmpfs")
        assert rows == [f"/tmp:size={APP_TMPFS_SIZE}"], (
            f"{name} /tmp must be tmpfs AND size-bounded, got {rows!r}"
        )
    # One image, one command difference — and one bound. A per-role tmpfs size is
    # how two roots of the same image come to fail differently under one load.
    assert len({tuple(services[name]["tmpfs"]) for name in APP_ROOTS}) == 1


def test_app_tmpfs_bound_fires_before_the_memory_cgroup_does() -> None:
    """The size= is small enough that the write fails BEFORE the cgroup kills.

    This is the arithmetic that picks the number, asserted rather than left in a
    comment: `tmpfs size + peak process anon < memory limit`. ne-api's measured
    peak anon is ~1145 MiB against a 2 GiB cap, which leaves ~903 MiB; 512 MiB
    fits with 391 MiB to spare, and the next round value up (1 GiB) does not fit
    at all — a 1g tmpfs would still let ne-api OOM and would buy nothing.
    """
    services = _services()
    for name in APP_ROOTS:
        limits = services[name]["deploy"]["resources"]["limits"]
        assert limits["memory"] == APP_MEMORY_LIMIT, name
        memory = _byte_count(limits["memory"])
        target, _, options = services[name]["tmpfs"][0].partition(":")
        assert target == "/tmp", name
        size = _byte_count(options.removeprefix("size="))
        assert options.startswith("size="), f"{name} tmpfs option must be a size, got {options!r}"
        peak_anon = NE_API_PEAK_ANON_MIB * 1024**2
        assert size + peak_anon < memory, (
            f"{name}: a {options} tmpfs plus the measured {NE_API_PEAK_ANON_MIB} MiB "
            f"resting anon exceeds the {limits['memory']} cgroup limit, so the cap "
            "would never surface as ENOSPC — the container would OOM first"
        )


def test_app_env_caps_glibc_arenas() -> None:
    """`MALLOC_ARENA_MAX=2` is set, and set on BOTH roots via the shared anchor.

    The host resource policy specified this lever and deferred it
    ("add it if a future burst reappears, not speculatively"). The burst
    reappeared: ne-api rests at 1133-1145 MiB anon with five fully resident
    64 MiB secondary arenas, grown by the audit-chain refresher's periodic
    O(chain) walk on anyio worker threads. glibc only returns memory at the top
    of an arena, so each fragmented arena stays mapped at its high-water mark.
    """
    services = _services()
    for name in APP_ROOTS:
        assert services[name]["environment"].get("MALLOC_ARENA_MAX") == "2", (
            f"{name} must cap the glibc arena pool"
        )


def test_glibc_arena_cap_is_outside_the_frozen_config_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The allocator tunable must NOT move the frozen platform ``config_hash``.

    ne-api and ne-worker must freeze the SAME hash (design §6.2/Q5), and a moved
    hash is a real deploy hazard on this box — the ``NE_GOVERN_OPA_URL``
    precedent. Proven two ways rather than asserted:

    * structurally — ``NeSettings`` is the only ``BaseSettings`` base in the
      tree, and it declares ``env_prefix="NE_"`` with ``extra="forbid"``, so a
      key with no ``NE_`` prefix cannot bind to any field of any settings model;
    * empirically — the composed platform hash is byte-identical with and
      without ``MALLOC_ARENA_MAX`` in the environment.

    The environment is touched ONLY via ``monkeypatch`` and only for this
    non-``NE_`` key, so nothing here perturbs another test's frozen config.
    """
    from ne.contracts.config import NeSettings, compute_platform_config_hash
    from ne.govern.config import GovernSettings
    from ne.runtime.config import RuntimeSettings

    assert NeSettings.model_config.get("env_prefix") == "NE_"
    assert NeSettings.model_config.get("extra") == "forbid"

    anchor = _services()["ne-api"]["environment"]
    non_prefixed = {key for key in anchor if not key.startswith("NE_")}
    assert non_prefixed == {"MALLOC_ARENA_MAX"}, (
        "a non-NE_ key in the shared app anchor is by construction invisible to "
        f"pydantic-settings; keep that set explicit, got {sorted(non_prefixed)}"
    )

    def _hash() -> str:
        return compute_platform_config_hash(
            {"ne-runtime": RuntimeSettings(), "ne-govern": GovernSettings()}
        )

    monkeypatch.delenv("MALLOC_ARENA_MAX", raising=False)
    without = _hash()
    monkeypatch.setenv("MALLOC_ARENA_MAX", anchor["MALLOC_ARENA_MAX"])
    with_var = _hash()
    assert without == with_var, "MALLOC_ARENA_MAX moved the frozen platform config_hash"


# --- NE-087 (O-4): the rebac profile --------------------------------------


def test_rebac_services_shape() -> None:
    services = _services()
    fga = services["openfga"]
    migrate = services["openfga-migrate"]
    # Digest-pinned to the patched-latest v1.18.3 (also covered by the digest-pin
    # guardian); the one-shot migrate shares the identical image.
    assert fga["image"] == REBAC_IMAGE
    assert migrate["image"] == REBAC_IMAGE
    # C1: playground + metrics telemetry explicitly off in the command.
    assert "--playground-enabled=false" in fga["command"]
    assert "--metrics-enabled=false" in fga["command"]
    # C12: preshared key is a required ${...:?} interpolation, never a literal.
    assert fga["environment"]["OPENFGA_AUTHN_PRESHARED_KEYS"].startswith(
        "${NE_PILOT_OPENFGA_API_TOKEN"
    )
    for svc in (fga, migrate):
        uri = svc["environment"]["OPENFGA_DATASTORE_URI"]
        assert "${NE_PILOT_POSTGRES_PASSWORD}" in uri
        assert "password" not in uri.lower().replace("${ne_pilot_postgres_password}", "")
    # Loopback-only HTTP API (gRPC 8081 stays unpublished); hardened uid + RO rootfs.
    assert fga["ports"] == ["127.0.0.1:8080:8080"]
    assert fga["user"] == "65532:65532"
    assert fga["read_only"] is True
    assert migrate["user"] == "65532:65532"
    assert migrate["read_only"] is True
    # depends_on: openfga waits on postgres healthy AND the migrate completing.
    assert fga["depends_on"]["postgres"] == {"condition": "service_healthy"}
    assert fga["depends_on"]["openfga-migrate"] == {"condition": "service_completed_successfully"}
    assert migrate["depends_on"]["postgres"] == {"condition": "service_healthy"}
    # grpc_health_probe healthcheck (the upstream image bundles the binary).
    assert "grpc_health_probe" in " ".join(fga["healthcheck"]["test"])


def test_rebac_app_env_keys_are_pass_through_null() -> None:
    # FIX-1 no-op-proof (static half): BOTH ne-api and ne-worker carry the five
    # NE_REBAC_* keys as NULL (compose omits them from the container when unset), and
    # no OTHER NE_REBAC_* key exists.
    services = _services()
    for name in ("ne-api", "ne-worker"):
        env = services[name]["environment"]
        for key in REBAC_APP_ENV_KEYS:
            assert key in env, f"{name} missing {key}"
            assert env[key] is None, f"{name}.{key} must be null (pass-through), got {env[key]!r}"
        extra = {k for k in env if k.startswith("NE_REBAC_")} - REBAC_APP_ENV_KEYS
        assert not extra, f"{name} has unexpected NE_REBAC_* keys: {extra}"


def test_default_profiles_have_no_openfga() -> None:
    # Off-by-default: every openfga* service sits behind the rebac profile, rebac is
    # never in the pilot expansion, and the included serve tier has no openfga.
    # (Pull the module from sys.modules — `from ne.cli import up` rebinds `up` to the
    # click Command, shadowing the submodule; see the test_dev_cli footgun note.)
    import sys

    up_module = sys.modules["ne.cli.up"]

    services = _services()
    for name, cfg in services.items():
        if name.startswith("openfga"):
            assert cfg.get("profiles") == ["rebac"], name
    assert "rebac" not in up_module._PILOT_EXPANSION
    serve = yaml.safe_load(
        (REPO_ROOT / "deploy" / "compose" / "serve" / "compose.yaml").read_text(encoding="utf-8")
    )
    assert not any(n.startswith("openfga") for n in (serve.get("services") or {}))


def test_tei_rerank_gets_the_cores_and_the_threads_to_use_them() -> None:
    """NE-RERANK-CPU: the rerank stage is the whole cost of a CPU-lane search.

    Measured 2026-08-27 on the 16-core demo box against the live corpus, 20
    candidates (the label then read "retrieval_top_k=5 x candidate_multiplier=4";
    that equation was false — the authorization wrapper's 8x made the shipped Ask
    160, and post NE-RERANK-FANOUT it is 40), mean 701 chars per
    chunk, median of 3 reps: **78.1 s** per search at the shipped cpus "4" /
    baked-in RAYON_NUM_THREADS 8, **31.2 s** at cpus "12" / RAYON_NUM_THREADS 12
    — with byte-identical top-5 ordering and scores. Both halves are load
    bearing and neither is inferable from the other, so both are pinned:

    * the cpu quota alone leaves rayon at the image's baked-in 8 (36.2 s), and
    * the thread count alone is throttled by the quota.

    cpus "14" measured WORSE (34.2 s) — 12 is the knee, chosen against the rest
    of the stack's needs, not a round number.
    """
    rerank = _services()["tei-rerank"]
    cpus = rerank["deploy"]["resources"]["limits"]["cpus"]
    assert str(cpus) == "12", "tei-rerank cpu limit is the search-latency lever"
    assert rerank["environment"]["RAYON_NUM_THREADS"] == str(cpus), (
        "RAYON_NUM_THREADS must match the cpu quota or the extra cores go unused"
    )
    # MAX_BATCH_TOKENS is deliberately NOT set here: measured no effect on
    # per-search cost (the Candle CPU backend forces max_batch_requests=4, so the
    # token budget was never binding) and a 16384 warm-up peaks at ~12 GiB against
    # the 4 GiB limit. The demo overlay's 2048 stays the operative value.
    assert "MAX_BATCH_TOKENS" not in rerank["environment"]


def test_qdrant_has_an_explicit_file_descriptor_ceiling() -> None:
    """Qdrant must NOT inherit the daemon's NOFILE default.

    With no `ulimits:` the container inherits dockerd's rlimits, whose SOFT
    NOFILE is 1024 (docker.service ships no LimitNOFILE, so systemd's
    DefaultLimitNOFILESoft applies), and Qdrant never raises its own soft limit.
    Qdrant holds descriptors open per SEGMENT: 986 collections x 2 segments each
    plus a per-shard WAL took the live demo's store to `status: red` with
    `Failed to remove segment: Too many open files (os error 24)`, after which
    every write died on `Can't write WAL: "no segments available"`. This pin is
    the ceiling, not the tuning -- see the derivation comment in compose.yaml.
    """
    nofile = _services()["qdrant"]["ulimits"]["nofile"]
    assert nofile["soft"] == nofile["hard"] == 262144, (
        "qdrant's NOFILE ceiling is a red-store guardrail, not a tunable knob"
    )


def test_telemetry_off_markers() -> None:
    services = _services()
    assert services["qdrant"]["environment"].get("QDRANT__TELEMETRY_DISABLED") == "true"
    assert "--disable-telemetry" in services["opa"]["command"]
    for name in ("tei-embed", "tei-rerank"):
        assert services[name]["environment"].get("HF_HUB_DISABLE_TELEMETRY") == "1"


def test_includes_serve_tier() -> None:
    data = _load()
    includes = data["include"]
    paths = [entry["path"] if isinstance(entry, dict) else entry for entry in includes]
    assert paths == ["../serve/compose.yaml"]


def test_no_openbao_or_langfuse_in_pilot() -> None:
    services = _services()
    for banned in ("openbao", "langfuse", "langfuse-web", "langfuse-worker"):
        assert banned not in services, f"{banned} must not be in the pilot tier"


def test_no_secret_literals_in_credentialed_env() -> None:
    # Every credential-shaped env var is a ${...} interpolation (belt-and-braces
    # with the dedicated hygiene guardian).
    cred = re.compile(r"(?i)(password|secret|token|_key)")
    for name, cfg in _services().items():
        for key, value in (cfg.get("environment") or {}).items():
            if cred.search(key):
                # NE-087 (O-4): a NULL-valued pass-through key (e.g. NE_REBAC_API_TOKEN
                # on the shared app anchor) carries NO value — it is populated only via
                # the ephemeral 0600 --env-file. That is C12-clean.
                assert value is None or (isinstance(value, str) and value.startswith("${")), (
                    f"{name}.{key} must be a ${{...}} interpolation or null, got {value!r}"
                )


# --- NE-103b: console same-origin serving, Caddy 443 vhost, pilot realm --------


def _all_mappers(realm: dict) -> list[dict]:
    mappers: list[dict] = []
    for scope in realm.get("clientScopes", []):
        mappers.extend(scope.get("protocolMappers", []))
    return mappers


def test_caddy_443_reverse_proxies_ne_api() -> None:
    for caddyfile in CADDYFILES:
        text = caddyfile.read_text(encoding="utf-8")
        assert "reverse_proxy ne-api:8000" in text, caddyfile
        assert "console arrives with NE-067" not in text, caddyfile
        # The Keycloak 8443 vhost and hardening are untouched.
        assert "reverse_proxy keycloak:8080" in text, caddyfile
        assert "admin off" in text, caddyfile


def test_keycloak_imports_the_pilot_realm() -> None:
    kc = _services()["keycloak"]
    assert "--import-realm" in kc["command"]
    assert "./keycloak:/opt/keycloak/data/import:ro" in kc["volumes"]
    assert "NE_PILOT_HOSTNAME" in kc["environment"]


def test_keycloak_entra_env_is_null_value_pass_through() -> None:
    # NE-BEW-INJECT: structural pin — the three optional Entra broker vars must
    # keep the NULL-VALUE pass-through form (same NE-087 NE_REBAC_* idiom, see
    # test_rebac_app_env_keys_are_pass_through_null above), never `${...:-}`
    # (that DEFINES the var as an empty string inside the container instead of
    # omitting it — the P2-A defect this pin exists to catch) and never `:?`
    # (that would parse-fail on absence — the fail-the-stack bug this unit
    # exists to prevent). yaml.safe_load renders a valueless mapping key as
    # None, so asserting `is None` pins out BOTH `:-` and `:?` regressions —
    # both would render as a non-None string — with one assertion per key.
    kc = _services()["keycloak"]
    for name in (
        "NE_PILOT_ENTRA_TENANT_ID",
        "NE_PILOT_ENTRA_BROKER_CLIENT_ID",
        "NE_PILOT_ENTRA_BROKER_CLIENT_SECRET",
    ):
        assert kc["environment"][name] is None, (
            f"{name} must be a null-value pass-through key (got "
            f"{kc['environment'][name]!r}) — see compose.yaml ~:201-207"
        )


def test_pilot_realm_client_shape() -> None:
    realm = _realm()
    assert realm["realm"] == "ne-pilot"
    assert realm["enabled"] is True
    clients = realm["clients"]
    assert len(clients) == 1
    client = clients[0]
    assert client["clientId"] == "ne-console"
    assert client["publicClient"] is True
    assert client["standardFlowEnabled"] is True
    assert client["implicitFlowEnabled"] is False
    assert "secret" not in client
    assert client["attributes"]["pkce.code.challenge.method"] == "S256"
    # Shape only: every redirect target is templated on the deploy hostname and
    # TLS-only. WHICH paths are declared is not asserted here — that set is
    # DERIVED from the console source and enforced bidirectionally by
    # test_oidc_redirect_claims.py (this test's previous hardcoded
    # `endswith("/auth/callback")` was the transliterated form that let the
    # unregistered /auth/compare-callback ship — the dead compare popup).
    for uri in client["redirectUris"]:
        assert "${NE_PILOT_HOSTNAME}" in uri
        assert uri.startswith("https://")
    assert "${NE_PILOT_HOSTNAME}" in client["attributes"]["post.logout.redirect.uris"]
    assert client["webOrigins"] == ["+"]
    idps = realm["identityProviders"]
    assert len(idps) == 1
    assert idps[0]["alias"] == "entra"
    assert idps[0]["enabled"] is False
    assert idps[0]["config"]["clientSecret"].startswith(
        "${"
    )  # NE-BEW-REALM: disabled-by-default Entra broker, secret is an interpolation (C12).


def test_pilot_realm_claims_and_roles_match_ne_api_env() -> None:
    realm = _realm()
    env = _services()["ne-api"]["environment"]
    assert env["NE_IDENTITY_OIDC__ISSUER"].endswith("/realms/ne-pilot")

    mappers = _all_mappers(realm)
    audience_mappers = [m for m in mappers if m["protocolMapper"] == "oidc-audience-mapper"]
    assert len(audience_mappers) == 1
    assert (
        env["NE_IDENTITY_OIDC__AUDIENCE"]
        == audience_mappers[0]["config"]["included.custom.audience"]
        == "ne-console"
    )

    groups_mappers = [
        m
        for m in mappers
        if m["protocolMapper"] == "oidc-group-membership-mapper"
        and m["config"].get("claim.name") == "groups"
    ]
    assert groups_mappers and groups_mappers[0]["config"]["full.path"] == "false"

    dept_mappers = [m for m in mappers if m["config"].get("claim.name") == "department"]
    assert len(dept_mappers) == 1, "exactly one department claim mapper must exist (SPEC §11 A1)"
    assert dept_mappers[0]["protocolMapper"] == "oidc-hardcoded-claim-mapper"
    assert dept_mappers[0]["config"]["claim.value"] == "engineering"

    realm_groups = {g["name"] for g in realm["groups"]}
    role_env = (
        json.loads(env["NE_API_APPROVER_ROLES"])
        + json.loads(env["NE_API_OPS_ROLES"])
        + json.loads(env["NE_API_STEWARD_ROLES"])
    )
    assert set(role_env) <= realm_groups, f"{set(role_env)} not subset of {realm_groups}"

    default_scopes = set(realm["clients"][0]["defaultClientScopes"])
    assert {"ne-groups", "ne-console-audience", "ne-department"} <= default_scopes


def test_pilot_realm_seeded_role_users() -> None:
    realm = _realm()
    users = {u["username"]: u for u in realm["users"]}
    assert set(users) == {"approver", "steward", "ops", "meridian-frontline"}
    # EVERY path a user claims must be a group this realm DECLARES. Keycloak does
    # not warn and does not partially import: an undeclared path aborts startup
    # with `Unable to find group specified by path: …`, so the demo comes up with
    # no identity provider at all. Verified live on the demo box, which
    # is how the missing `/all-employees` declaration was caught.
    declared = {g["path"] for g in realm["groups"]}
    for name, user in users.items():
        assert user["enabled"] is True
        assert user["emailVerified"] is True
        # The role group is the persona; membership beyond it is what that persona
        # can READ. `ops` also holds `/all-employees` because the operator is an
        # Acme employee — the Acme corpus gates its public documents on that ACL
        # class, so an operator without it signs in and can read nothing.
        assert f"/{name}" in user["groups"]
        assert set(user["groups"]) <= declared, (name, set(user["groups"]) - declared)
        creds = user.get("credentials", [])
        if name == "meridian-frontline":
            assert creds == [], "restricted persona credentials must be generated by ne demo up"
        else:
            assert len(creds) == 1
            assert creds[0]["type"] == "password"
            assert creds[0]["temporary"] is False
            # Documented demo string shape only (do not pin the literal beyond this).
            assert creds[0]["value"] == f"{name}-demo"

    assert set(users["meridian-frontline"]["groups"]) == {
        "/all-employees",
        "/meridian-frontline",
        "/bank.customer.read",
        "/bank.account-summary.read",
        "/sharepoint.frontline.read",
    }
    assert set(users["ops"]["groups"]) == {
        "/all-employees",
        "/ops",
        "/meridian-ops",
        "/bank.customer.read",
        "/bank.servicing.read",
        "/bank.dispute.read",
        "/sharepoint.operations.read",
        # NE-CS-SPLIT resilience: the DERIVED Skill ACL label is seeded in the
        # committed realm too, so a FRESH realm import (wiped Keycloak DB) is
        # correct before provisioning re-runs `project_servicing_skill_authority`
        # — a restart-dropped projection broke every Skill-pinned run 2026-08-27.
        # The projector remains the authority: it reconciles this membership to
        # exactly the `bank.servicing.read` set on every `ne demo up`.
        "/department:servicing",
    }


def test_ne_api_serves_console_same_origin() -> None:
    env = _services()["ne-api"]["environment"]
    assert env["NE_API_SERVE_CONSOLE_DIST"] == "/app/console-dist"
    assert (
        env["NE_IDENTITY_OIDC__JWKS_URI"]
        == "http://keycloak:8080/realms/ne-pilot/protocol/openid-connect/certs"
    )
    assert env["NE_IDENTITY_OIDC__ALLOW_INSECURE_HTTP"] == "true"
    caddy_deps = _services()["caddy"]["depends_on"]
    assert caddy_deps["ne-api"] == {"condition": "service_healthy"}
