# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-086 — the Zarf air-gap package PARITY GUARDIAN.

Static tests (always run, no helm needed) assert the Zarf package definition
(deploy/airgap/zarf/zarf.yaml) is well-formed and that its SHARED third-party
image pins are byte-copies of the chart AND compose pins (C11 triple parity), that
the FIRST-PARTY image is a create-time template (owner dec ii, never a committed
fake digest), that no secret literal lives in any zarf source (C12), and that
the workflow's Zarf version pins are internally consistent.

One render test (per-test `_needs_helm` skipif — mirrors test_helm_chart.py's
scoping, so the static guardians NEVER skip just because helm is absent) is THE
SPEC-mandated guardian: zarf.yaml's image set == the chart's rendered image set,
nothing missing, nothing extra. CI's zarf-package.yml installs helm and gates on
this test having actually EXECUTED (must-run-not-skip).
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
ZARF_DIR = REPO_ROOT / "deploy" / "airgap" / "zarf"
ZARF_YAML = ZARF_DIR / "zarf.yaml"
CHART_DIR = REPO_ROOT / "deploy" / "helm" / "neutron-enterprise"
COMPOSE_FILE = REPO_ROOT / "deploy" / "compose" / "pilot" / "compose.yaml"
VALUES_CI = REPO_ROOT / "deploy" / "helm" / "tests" / "values-ci.yaml"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "zarf-package.yml"
ZARF_README = ZARF_DIR / "README.md"

_DIGEST_PIN = re.compile(r"^[^@\s]+:[^@\s]+@sha256:[0-9a-f]{64}$")
_NE_APP_TOKEN = "###ZARF_PKG_TMPL_NE_APP_IMAGE###"
# The fixture first-party digest used by values-ci.yaml (all-zero sha256).
_FIXTURE_NE_APP = "ne-app:0.1.0@sha256:" + "0" * 64
_PILOT_CUTOVER_FILES = {
    "../../../tools/ne-cli": "/opt/neutron/pilot-cutover/tools/ne-cli",
    "../../../tools/ne-backup": "/opt/neutron/pilot-cutover/tools/ne-backup",
    "../../compose/pilot/compose.yaml": (
        "/opt/neutron/pilot-cutover/deploy/compose/pilot/compose.yaml"
    ),
}


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _zarf() -> dict[str, Any]:
    return _load_yaml(ZARF_YAML)


def _zarf_component() -> dict[str, Any]:
    components = _zarf()["components"]
    assert len(components) == 1, f"expected exactly one component, got {len(components)}"
    return components[0]


def _zarf_images() -> list[str]:
    return list(_zarf_component()["images"])


def _compose_services() -> dict[str, dict]:
    return _load_yaml(COMPOSE_FILE)["services"]


def _chart_values() -> dict[str, Any]:
    return _load_yaml(CHART_DIR / "values.yaml")


def _tracked_zarf_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "deploy/airgap/zarf"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [REPO_ROOT / rel for rel in out]


# ---------------------------------------------------------------------------
# Static tests — no helm binary required
# ---------------------------------------------------------------------------
def test_zarf_yaml_layout_and_chart_ref() -> None:
    doc = _zarf()
    assert doc["kind"] == "ZarfPackageConfig"
    assert doc["metadata"]["name"] == "neutron-enterprise"

    component = _zarf_component()
    assert component["required"] is True

    charts = component["charts"]
    assert len(charts) == 1
    chart = charts[0]
    assert chart["name"] == "neutron-enterprise"
    # localPath is relative to the zarf.yaml directory.
    resolved = (ZARF_DIR / chart["localPath"]).resolve()
    assert resolved == CHART_DIR.resolve(), f"localPath resolves to {resolved}, not the chart dir"
    assert resolved.is_dir()

    # Chart version read LIVE from Chart.yaml (never hardcoded here).
    chart_version = str(_load_yaml(CHART_DIR / "Chart.yaml")["version"])
    assert str(chart["version"]) == chart_version, "zarf chart version != Chart.yaml version"

    assert chart["namespace"] == "neutron"
    for values_file in chart["valuesFiles"]:
        assert (ZARF_DIR / values_file).is_file(), f"missing valuesFile {values_file}"
    assert (CHART_DIR / "files/agents/registry.toml").read_bytes() == (
        REPO_ROOT / "deploy/agents/registry.toml"
    ).read_bytes()
    assert (CHART_DIR / "files/skills/meridian-hardship-draft/SKILL.md").read_bytes() == (
        REPO_ROOT / "deploy/skills/meridian-hardship-draft/SKILL.md"
    ).read_bytes()
    assert all("deploy/agents" not in item["source"] for item in component.get("files", [])), (
        "Agent registry must not gain a second Zarf source"
    )


def test_zarf_packages_exact_pilot_transport_cutover_kit() -> None:
    component = _zarf_component()
    files = component.get("files")
    assert isinstance(files, list)
    actual = {item["source"]: item["target"] for item in files}
    assert actual == _PILOT_CUTOVER_FILES

    for source in actual:
        resolved = (ZARF_DIR / source).resolve()
        assert resolved.exists(), f"missing Pilot cutover package source: {source}"
        assert resolved.is_relative_to(REPO_ROOT)

    backup_project = tomllib.loads(
        (REPO_ROOT / "tools" / "ne-backup" / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert backup_project["project"]["scripts"]["ne-backup"] == "ne.backup.cli:main"


def test_zarf_third_party_images_match_their_deployment_admission_shapes() -> None:
    services = _compose_services()
    values = _chart_values()
    zarf_images = _zarf_images()
    # Keycloak/OPA remain one third-party pin across Helm, Zarf and Compose.
    # Pilot PostgreSQL is now the separately packaged first-party AGE artifact,
    # while Kubernetes retains its existing chart/Zarf PostgreSQL pin until a
    # registry-promoted AGE manifest exists; neither side may impersonate the
    # other's admission shape.
    pairs = {"keycloak": "keycloak", "opa": "opa"}
    for chart_key, compose_name in pairs.items():
        image = values[chart_key]["image"]
        expected = f"{image['repository']}:{image['tag']}@{image['digest']}"
        assert _DIGEST_PIN.match(expected), f"chart {chart_key} not digest-pinned: {expected!r}"
        # zarf == chart
        assert expected in zarf_images, f"zarf.yaml missing chart pin for {chart_key}: {expected!r}"
        # zarf == compose (the executable spec)
        compose_image = services[compose_name]["image"]
        assert compose_image == expected, (
            f"{chart_key}: compose {compose_image!r} != chart-composed {expected!r}"
        )

    postgres = values["postgres"]["image"]
    zarf_postgres = f"{postgres['repository']}:{postgres['tag']}@{postgres['digest']}"
    assert zarf_postgres in zarf_images
    assert services["postgres"]["image"] == (
        "sha256:6ba66e01b43cbfde0f0a03699f609fed1e218612aab7b2119052e0fd5aeb4f26"
    )
    assert services["postgres"]["image"] != zarf_postgres


def test_zarf_first_party_image_create_time_templated() -> None:
    images = _zarf_images()
    # NE-071c: the default render grew by qdrant + tei (4 → 6 entries).
    assert len(images) == 6, f"expected exactly 6 image entries, got {len(images)}: {images}"
    assert images.count(_NE_APP_TOKEN) == 1, "exactly one entry must be the ne-app create token"

    # constants wire NE_APP_REPOSITORY/NE_APP_DIGEST to package-template tokens.
    constants = {c["name"]: c["value"] for c in _zarf()["constants"]}
    assert constants["NE_APP_REPOSITORY"] == "###ZARF_PKG_TMPL_NE_APP_REPOSITORY###"
    assert constants["NE_APP_DIGEST"] == "###ZARF_PKG_TMPL_NE_APP_DIGEST###"

    overlay = _load_yaml(ZARF_DIR / "values" / "zarf-overlay.yaml")
    image = overlay["global"]["image"]
    assert image["repository"] == "###ZARF_CONST_NE_APP_REPOSITORY###"
    assert image["digest"] == "###ZARF_CONST_NE_APP_DIGEST###"
    # Tag byte-matches the chart's first-party tag (owner dec ii: only the
    # digest is create-time injected — never a committed fake literal digest).
    assert str(image["tag"]) == str(_chart_values()["global"]["image"]["tag"])


# --- C12 secret-literal scanner: a LOCAL COPY of test_helm_chart.py's
# `_literal_credential_assignments` (deliberately duplicated — the tests/ dir
# has no package __init__, and copying keeps the diff isolated to this unit).
_CRED_ASSIGNMENT = re.compile(
    r"(?im)^[^#\n]*?\b(?P<key>[A-Za-z0-9_]*(?:password|secret|token|_key)[A-Za-z0-9_]*)"
    r"[ \t]*[:=][ \t]*(?P<val>[^\s#]+)"
)


def _literal_credential_assignments(text: str) -> list[tuple[str, str]]:
    offenders: list[tuple[str, str]] = []
    for match in _CRED_ASSIGNMENT.finditer(text):
        key = match.group("key")
        if key.lower().endswith("name"):
            continue
        val = match.group("val").strip().strip("\"'")
        if val.startswith("${") or val.startswith("$(") or val.startswith("{{"):
            continue
        offenders.append((key, val))
    return offenders


def test_zarf_scanner_positive_control() -> None:
    planted = "password: hunter2-not-an-interpolation\n"
    assert _literal_credential_assignments(planted) == [
        ("password", "hunter2-not-an-interpolation")
    ]
    assert _literal_credential_assignments("secretName: ne-platform-secrets\n") == []


def test_no_secret_literals_in_zarf_sources() -> None:
    for path in _tracked_zarf_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        offenders = _literal_credential_assignments(text)
        assert offenders == [], f"literal secret assignment(s) in {path}: {offenders}"
        assert "BEGIN PRIVATE KEY" not in text, path


def test_zarf_version_pins_consistent() -> None:
    wf = WORKFLOW.read_text(encoding="utf-8")
    env: dict[str, str] = {}
    for key in ("ZARF_VERSION", "ZARF_SHA256", "ZARF_INIT_SHA256"):
        match = re.search(rf'(?m)^\s*{key}:\s*"?([^"\s]+)"?\s*$', wf)
        assert match, f"{key} not found in {WORKFLOW}"
        env[key] = match.group(1)
        assert env[key], f"{key} is empty"
    # Doc/workflow drift lock: the pinned version is named in the README.
    assert env["ZARF_VERSION"] in ZARF_README.read_text(encoding="utf-8"), (
        f"ZARF_VERSION {env['ZARF_VERSION']} not documented in {ZARF_README}"
    )


# ---------------------------------------------------------------------------
# Render test — requires a local `helm` binary (per-test skipif, NOT
# module-level — the static guardians above must never be skipped for lack of
# helm). CI's zarf-package.yml asserts this test EXECUTED (must-run-not-skip).
# ---------------------------------------------------------------------------
_needs_helm = pytest.mark.skipif(shutil.which("helm") is None, reason="helm not installed")


def _rendered_images(*extra: str) -> set[str]:
    cmd = ["helm", "template", "ne", str(CHART_DIR), "-f", str(VALUES_CI), *extra]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    images: set[str] = set()
    for doc in yaml.safe_load_all(result.stdout):
        if doc:
            _collect_images(doc, images)
    return images


def _collect_images(doc: Any, images: set[str]) -> None:
    """Walk any rendered doc, collecting every containers[]/initContainers[]
    image (incl. a CronJob's nested jobTemplate pod spec)."""
    if isinstance(doc, dict):
        for key, value in doc.items():
            if key in ("containers", "initContainers") and isinstance(value, list):
                for container in value:
                    if isinstance(container, dict) and "image" in container:
                        images.add(container["image"])
            else:
                _collect_images(value, images)
    elif isinstance(doc, list):
        for item in doc:
            _collect_images(item, images)


@_needs_helm
def test_zarf_images_equal_chart_rendered_set() -> None:
    # Union the default render with the backup CronJob render (its pg_dumpall
    # container reuses the already-pinned postgres image — no new image, but
    # exercised so a future backup image change would break here too).
    rendered = _rendered_images() | _rendered_images("--set", "backup.enabled=true")

    zarf_images = _zarf_images()
    zarf_third_party = {img for img in zarf_images if img != _NE_APP_TOKEN}
    assert _NE_APP_TOKEN in zarf_images

    # The rendered first-party ref (fixture digest) corresponds to the single
    # templated zarf entry; every OTHER rendered image is a literal zarf pin.
    rendered_third_party = {img for img in rendered if not img.startswith("ne-app:")}
    rendered_first_party = {img for img in rendered if img.startswith("ne-app:")}

    assert rendered_third_party == zarf_third_party, (
        f"zarf third-party set != chart rendered set\n"
        f"  missing from zarf: {sorted(rendered_third_party - zarf_third_party)}\n"
        f"  extra in zarf:     {sorted(zarf_third_party - rendered_third_party)}"
    )
    # Exactly one first-party rendered image, and it is the CI fixture ref that
    # the ###ZARF_PKG_TMPL_NE_APP_IMAGE### token stands in for.
    assert rendered_first_party == {_FIXTURE_NE_APP}, rendered_first_party


def test_zarf_new_core_images_byte_match_chart_and_compose() -> None:
    """NE-071c: qdrant + tei (the two new DEFAULT-render images) are byte-copies
    of the chart values AND the pilot compose pins, and are present in zarf.yaml.
    Negative assert: otel-collector + vllm are DELIBERATELY absent — obs/serve
    are default-OFF profiles, so they are not part of the default render (== the
    vendored set); the byte-parity guardian equality is against that default set.
    """
    services = _compose_services()
    values = _chart_values()
    zarf_images = _zarf_images()

    # qdrant ← chart values == pilot compose == present in zarf.
    q_image = values["qdrant"]["image"]
    q_expected = f"{q_image['repository']}:{q_image['tag']}@{q_image['digest']}"
    assert _DIGEST_PIN.match(q_expected), q_expected
    assert q_expected == services["qdrant"]["image"]
    assert q_expected in zarf_images, f"zarf.yaml missing qdrant pin: {q_expected!r}"

    # tei (shared pin) ← chart values == pilot tei-embed AND tei-rerank == in zarf.
    t_image = values["tei"]["image"]
    t_expected = f"{t_image['repository']}:{t_image['tag']}@{t_image['digest']}"
    assert _DIGEST_PIN.match(t_expected), t_expected
    assert t_expected == services["tei-embed"]["image"]
    assert t_expected == services["tei-rerank"]["image"]
    assert t_expected in zarf_images, f"zarf.yaml missing tei pin: {t_expected!r}"

    # Negative: no obs/serve images vendored (default-OFF profiles).
    for img in zarf_images:
        assert "opentelemetry-collector" not in img, f"otel must NOT be vendored: {img}"
        assert "vllm" not in img, f"vllm must NOT be vendored: {img}"
