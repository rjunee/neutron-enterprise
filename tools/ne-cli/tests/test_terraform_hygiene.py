# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""C11/C12 hygiene guardian for deploy/terraform-azure + deploy/terraform-eks
(NE-072).

Proves: (C12) no git-tracked file under either terraform tier carries a
literal secret value, and no state/tfvars artifact is ever committed;
(C11) every provider is exact-pinned, both provider lock files are committed
with hash entries, and every upstream module `source` is a `git::` URL
pinned to a full 40-hex commit SHA; and that the owner-gated /
C2-posture-critical variable defaults documented in the design (gpu pool,
managed postgres, Velero install, frontier FQDN allowlist, egress mode)
have not silently drifted.

Provider parsing is NAMESPACE-AWARE (NE-LP-AZURE-VLW-FIX): a provider
`source` may live under any registry namespace, not just `hashicorp/` — e.g.
`deploy/terraform-azure/versions.tf` declares `Azure/azapi` for the VLW
(version-level immutability) WORM container fix. `_required_providers_versions`
returns `{name: (namespace, version)}` so the exact-pin and lock-hash checks
below cover every provider regardless of namespace.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
AZURE_DIR = REPO_ROOT / "deploy" / "terraform-azure"
EKS_DIR = REPO_ROOT / "deploy" / "terraform-eks"
AZURE_REL = "deploy/terraform-azure"
EKS_REL = "deploy/terraform-eks"

# A credential-shaped assignment to a LITERAL quoted string — the shape a
# leaked secret would take in HCL. Variable references (var.x, resource
# attribute references, `false`/unquoted values) never match this pattern
# because they are not quoted string literals; only the assignment KEY needs
# to carry the credential word (mirrors test_pilot_secret_hygiene.py's
# _CRED_ASSIGNMENT design, copied not imported).
_CRED_ASSIGNMENT = re.compile(
    r"(?im)^[^#\n]*?\b(?P<key>[A-Za-z0-9_]*(?:password|secret|token|_key)[A-Za-z0-9_]*)"
    r"\s*=\s*\"(?P<val>[^\"]*)\""
)

_GIT_SOURCE = re.compile(r'source\s*=\s*"git::[^"]*"')
_GIT_SOURCE_REF = re.compile(r'source\s*=\s*"git::[^"]*\?ref=(?P<ref>[^"&]+)"')
_SHA40 = re.compile(r"^[0-9a-f]{40}$")

_PROVIDER_VERSION = re.compile(r'^\s*version\s*=\s*"(?P<version>[^"]+)"', re.MULTILINE)
_EXACT_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_PROVIDER_SOURCE = re.compile(
    r'^\s*source\s*=\s*"(?P<ns>[A-Za-z][A-Za-z0-9]*)/(?P<name>[a-z][a-z0-9]*)"', re.MULTILINE
)


def _tracked_files(rel: str) -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", rel],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [REPO_ROOT / rel_path for rel_path in out]


def _all_tracked_terraform_files() -> list[Path]:
    return _tracked_files(AZURE_REL) + _tracked_files(EKS_REL)


def _literal_credential_assignments(text: str) -> list[tuple[str, str]]:
    return [(m.group("key"), m.group("val")) for m in _CRED_ASSIGNMENT.finditer(text)]


def test_detector_catches_a_planted_literal() -> None:
    # Positive control: the scanner must flag an obvious leak.
    planted = 'postgres_admin_password = "hunter2-not-a-var-reference"\n'
    assert _literal_credential_assignments(planted) == [
        ("postgres_admin_password", "hunter2-not-a-var-reference")
    ]
    # And must NOT flag a variable reference or a non-credential key.
    assert _literal_credential_assignments("admin_password = var.postgres_admin_password\n") == []
    assert _literal_credential_assignments('name = "entra-broker-client-secret"\n') == []


def test_no_secret_literal_in_terraform_files() -> None:
    for path in _all_tracked_terraform_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        offenders = _literal_credential_assignments(text)
        assert offenders == [], f"literal secret assignment(s) in {path}: {offenders}"


def test_no_state_or_tfvars_committed() -> None:
    for path in _all_tracked_terraform_files():
        name = path.name
        assert not name.startswith(".terraform.tfstate"), f"{path} must not be committed"
        assert "tfstate" not in name, f"{path} must not be committed"
        if name.endswith(".tfvars"):
            assert name.endswith(".tfvars.example"), f"{path}: only *.tfvars.example may be tracked"


def _required_providers_versions(versions_tf: Path) -> dict[str, tuple[str, str]]:
    text = versions_tf.read_text(encoding="utf-8")
    # Pair each `source = "<namespace>/x"` with the `version = "..."` that
    # follows it inside the same required_providers entry.
    entries = [(m.group("ns"), m.group("name")) for m in _PROVIDER_SOURCE.finditer(text)]
    versions = [m.group("version") for m in _PROVIDER_VERSION.finditer(text)]
    assert len(entries) == len(versions), f"{versions_tf}: provider/version count mismatch"
    return {name: (ns, version) for (ns, name), version in zip(entries, versions, strict=True)}


def test_provider_versions_exact_pinned() -> None:
    for versions_tf in (AZURE_DIR / "versions.tf", EKS_DIR / "versions.tf"):
        providers = _required_providers_versions(versions_tf)
        assert providers, f"{versions_tf}: no required_providers found (vacuous check)"
        for name, (_ns, version) in providers.items():
            assert _EXACT_VERSION.match(version), (
                f"{versions_tf}: provider {name} version {version!r} is not an exact x.y.z pin"
            )


def test_provider_lock_files_committed_with_hashes() -> None:
    for root, versions_tf in (
        (AZURE_DIR, AZURE_DIR / "versions.tf"),
        (EKS_DIR, EKS_DIR / "versions.tf"),
    ):
        lock_path = root / ".terraform.lock.hcl"
        assert lock_path.is_file(), f"{lock_path} must be committed"
        tracked = subprocess.run(
            ["git", "ls-files", str(lock_path.relative_to(REPO_ROOT))],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert tracked, f"{lock_path} exists on disk but is not git-tracked"

        lock_text = lock_path.read_text(encoding="utf-8")
        providers = _required_providers_versions(versions_tf)
        for name, (ns, _version) in providers.items():
            assert f'provider "registry.terraform.io/{ns.lower()}/{name}"' in lock_text, (
                f"{lock_path}: missing lock entry for provider {name}"
            )
        assert "h1:" in lock_text, f"{lock_path}: no h1: hash entries found"


def test_azure_declares_azapi_for_vlw() -> None:
    # Regression trip-wire (NE-LP-AZURE-VLW-FIX): the VLW WORM container fix
    # depends on the azapi provider being declared. If a future edit drops
    # it, this must fail loudly rather than let the defect silently return.
    providers = _required_providers_versions(AZURE_DIR / "versions.tf")
    assert "azapi" in providers, (
        f"{AZURE_DIR / 'versions.tf'}: must declare Azure/azapi (required for the VLW WORM "
        "container AzureBlobObjectStore.archive() preflights, NE-LP-AZURE-VLW-FIX)"
    )
    namespace, version = providers["azapi"]
    assert namespace == "Azure", (
        f"azapi provider must be sourced from namespace 'Azure', got {namespace!r}"
    )
    assert _EXACT_VERSION.match(version), (
        f"azapi provider version {version!r} is not an exact x.y.z pin"
    )


def test_module_sources_git_sha_pinned() -> None:
    found_any = False
    for path in _all_tracked_terraform_files():
        if path.suffix != ".tf":
            continue
        text = path.read_text(encoding="utf-8")
        for m in _GIT_SOURCE.finditer(text):
            found_any = True
            line = m.group(0)
            ref_match = _GIT_SOURCE_REF.search(line)
            assert ref_match, f"{path}: git:: module source has no ?ref=: {line}"
            ref = ref_match.group("ref")
            assert _SHA40.match(ref), (
                f"{path}: git:: module source ref {ref!r} is not a full 40-hex commit SHA"
            )
    assert found_any, "no git:: module sources found (vacuous check — expected in terraform-eks)"


def _variable_block(text: str, name: str) -> str:
    marker = f'variable "{name}"'
    start = text.index(marker)
    brace_start = text.index("{", start)
    depth = 0
    i = brace_start
    for i in range(brace_start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                break
    return text[brace_start : i + 1]


def _variable_default(variables_tf: Path, name: str) -> str:
    text = variables_tf.read_text(encoding="utf-8")
    block = _variable_block(text, name)
    m = re.search(r"default\s*=\s*(?P<val>.+)", block)
    assert m, f"variable {name!r} in {variables_tf} has no default"
    return m.group("val").strip()


def test_gpu_pool_default_off() -> None:
    assert _variable_default(AZURE_DIR / "variables.tf", "gpu_node_pool") == "false"


def test_managed_postgres_default_off() -> None:
    assert _variable_default(AZURE_DIR / "variables.tf", "managed_postgres") == "false"


def test_install_velero_default_off() -> None:
    assert _variable_default(AZURE_DIR / "variables.tf", "install_velero") == "false"


def test_frontier_fqdns_default_single_lane() -> None:
    default = _variable_default(AZURE_DIR / "variables.tf", "frontier_fqdns")
    assert default == '["api.anthropic.com"]'


def test_egress_mode_default_firewall() -> None:
    assert _variable_default(AZURE_DIR / "variables.tf", "egress_mode") == '"firewall"'


def test_create_entra_broker_default_on() -> None:
    # The smoke-lane override (azure-live-apply.yml sets this false) must
    # stay an override — the shipped customer default creates the broker
    # (NE-LP-AZURE-KV-REACH).
    assert _variable_default(AZURE_DIR / "variables.tf", "create_entra_broker") == "true"


def test_eks_managed_postgres_default_off() -> None:
    assert _variable_default(EKS_DIR / "variables.tf", "managed_postgres") == "false"


def test_eks_install_velero_default_off() -> None:
    assert _variable_default(EKS_DIR / "variables.tf", "install_velero") == "false"


def test_eks_postgres_admin_password_sensitive_no_default() -> None:
    text = (EKS_DIR / "variables.tf").read_text(encoding="utf-8")
    block = _variable_block(text, "postgres_admin_password")
    assert re.search(r"default\s*=\s*null", block)
    assert re.search(r"sensitive\s*=\s*true", block)
