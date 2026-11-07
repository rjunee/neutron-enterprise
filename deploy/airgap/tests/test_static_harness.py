# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Always-on static guardian for the NE-073 no-egress CI harness.

Runs ANYWHERE (no live namespace, no docker daemon, no privileges needed) via
``uv run pytest deploy/airgap/tests`` — including in this unit's own local
gate loop and, as its very first step, inside the `no-egress` job itself.
Enforces that the harness stays internally consistent WITHOUT ever requiring
a live run: shell scripts parse, the workflow's step ordering + action pins
can't silently drift, the CI resource-shape overlay can never widen the
pilot's security/image contract, and every phase module keeps its
NE_AIRGAP fail-closed gate.
"""

from __future__ import annotations

import base64
import re
import subprocess
from pathlib import Path

import yaml
from deploy.airgap.scripts.make_ci_bundle import build_ci_secret_payloads

from ne.cli.pilotcfg import REQUIRED_SECRETS

REPO_ROOT = Path(__file__).resolve().parents[3]
AIRGAP_DIR = REPO_ROOT / "deploy" / "airgap"
SCRIPTS_DIR = AIRGAP_DIR / "scripts"
TESTS_DIR = AIRGAP_DIR / "tests"
WORKFLOW_FILE = REPO_ROOT / ".github" / "workflows" / "no-egress.yml"
CI_WORKFLOW_FILE = REPO_ROOT / ".github" / "workflows" / "ci.yml"
README_FILE = AIRGAP_DIR / "README.md"
OVERLAY_FILE = AIRGAP_DIR / "compose.ci.yaml"
PILOT_COMPOSE_FILE = REPO_ROOT / "deploy" / "compose" / "pilot" / "compose.yaml"
ZARF_FILE = REPO_ROOT / "deploy" / "airgap" / "zarf" / "zarf.yaml"
SERVE_COMPOSE_FILE = REPO_ROOT / "deploy" / "compose" / "serve" / "compose.yaml"
SKILL_FILE = REPO_ROOT / "deploy" / "skills" / "meridian-hardship-draft" / "SKILL.md"

_SHA_PIN = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
_DIGEST_PIN = re.compile(r"@sha256:[0-9a-f]{64}")
_COPYRIGHT_MARKER = "Copyright (c)"
_OWNER_MARKER = "Ryan Junee"
_HEADER_SCAN_LINES = 5

_STAGE_STEP = "Stage: netns + inner dockerd"
_BUILD_STEP = "Stage: build ne-app image"
_AGE_BUILD_STEP = "Stage: build admitted PostgreSQL + AGE image"
_PULL_STEP = "Stage: pull stack images"
_SEAL_STEP = "SEAL (point of no return)"
_PHASE_A_STEP = "Phase A: air-gapped install via real installer"
_PHASE_C_STEP = "Phase C: open-weight lane (sealed)"
_PHASE_C_NOTICE_STEP = (
    "Phase C: deselected on pull_request (open-weight lane runs on push/schedule/dispatch)"
)

# NE-CI-NOEGRESS-VLLM-FLAKE: these are the load-bearing steps that must run in
# EVERY lane (PR and full) — the seal/egress assertions this gate exists for.
# Conditionalizing any of them is exactly the failure mode this unit forbids.
_ALWAYS_BLOCKING_STEPS = (
    "Prove egress dead (must FAIL closed)",
    _PHASE_A_STEP,
    "Phase A: in-namespace TLS smoke",
    "Phase A: container canary via the inner dockerd default bridge (must FAIL closed)",
    "Phase B1: brain qdrant + tei-embed (sealed, offline)",
    "Phase B2: brain tei-rerank (sealed, offline)",
    _SEAL_STEP,
    "Re-assert seal + report counters",
)


# ---------------------------------------------------------------------------
# (a) every shell script parses
# ---------------------------------------------------------------------------
def _all_scripts() -> list[Path]:
    return sorted(SCRIPTS_DIR.glob("*.sh"))


def test_all_scripts_exist() -> None:
    assert _all_scripts(), "expected at least one deploy/airgap/scripts/*.sh"


def test_every_script_bash_parses() -> None:
    for script in _all_scripts():
        result = subprocess.run(
            ["bash", "-n", str(script)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, f"{script.name} failed to parse: {result.stderr}"


# ---------------------------------------------------------------------------
# (b) workflow structure
# ---------------------------------------------------------------------------
def _load_workflow() -> dict:
    with WORKFLOW_FILE.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _steps() -> list[dict]:
    data = _load_workflow()
    return data["jobs"]["no-egress"]["steps"]


def _step_names() -> list[str]:
    return [s.get("name", "") for s in _steps()]


def test_workflow_parses() -> None:
    data = _load_workflow()
    assert "no-egress" in data["jobs"]


def test_permissions_contents_read() -> None:
    data = _load_workflow()
    assert data["permissions"] == {"contents": "read"}
    assert data["jobs"]["no-egress"]["permissions"] == {"contents": "read"}


def test_required_steps_present_and_ordered() -> None:
    names = _step_names()
    for required in (
        _STAGE_STEP,
        _BUILD_STEP,
        _AGE_BUILD_STEP,
        _PULL_STEP,
        _SEAL_STEP,
        _PHASE_A_STEP,
        _PHASE_C_STEP,
    ):
        assert required in names, f"missing required step: {required}"
    assert names.index(_STAGE_STEP) < names.index(_SEAL_STEP), "stage must precede seal"
    # The first-party image is built during staging (egress up), BEFORE the seal.
    assert names.index(_STAGE_STEP) < names.index(_BUILD_STEP), "stage must precede the build"
    assert names.index(_BUILD_STEP) < names.index(_SEAL_STEP), "build must precede the seal"
    assert names.index(_STAGE_STEP) < names.index(_AGE_BUILD_STEP)
    assert names.index(_AGE_BUILD_STEP) < names.index(_PULL_STEP) < names.index(_SEAL_STEP)
    assert names.index(_SEAL_STEP) < names.index(_PHASE_A_STEP), (
        "seal must precede the air-gapped install"
    )
    assert names.index(_PHASE_A_STEP) < names.index(_PHASE_C_STEP), "phase A must precede phase C"


def test_build_step_uses_inner_dockerd_and_iidfile() -> None:
    build = next(s for s in _steps() if s.get("name") == _BUILD_STEP)
    run = build.get("run", "")
    assert "NE_AIRGAP_DOCKER_HOST" in run, "build must target the inner dockerd control socket"
    assert "build-ne-app.sh" in run, "build must invoke build-ne-app.sh"
    assert "--iidfile" in run, "build must capture the local image id via --iidfile"


def test_age_build_and_phase_a_bind_exact_ci_local_admission() -> None:
    age_build = next(s for s in _steps() if s.get("name") == _AGE_BUILD_STEP).get("run", "")
    # Line continuations joined and whitespace collapsed, so each shell command
    # is ONE logical line that can be compared whole. Containment cannot forbid
    # an extension of the line it matches: asserting only the `git rev-parse`
    # prefix left the `= "$admitted_tree"` comparison deletable with every
    # owning test green (Argus P2-1 on PR #1094). V1, fixed-form gallery row 1.
    logical_lines = [" ".join(line.split()) for line in age_build.replace("\\\n", " ").splitlines()]
    assert [line for line in logical_lines if line.startswith("admitted_tree=")] == [
        'admitted_tree="8b60ae265b5d08a794a489492e7709f0da775734"'
    ]
    assert "admitted_source" not in age_build
    assert "6e28eb71ba23da3905e67d00f2fa2a9893f38ed0" not in age_build
    assert "c02acaa91c62ee9a72a22dd1f96cc3459857a4c5" not in age_build
    assert "52fac00d4dfe0c4556e556f62341184cc9a73ada" not in age_build
    assert "247bc0c99c9f7c1445f1efa74d0f95cfca1d02e6" not in age_build
    assert "6900b37be82a3afba8d95dc454912331e5dc2123" not in age_build
    assert "a9de1ebd228605384736d6edcb00a903a118c215" not in age_build
    assert "a2dd770190fedfc80b51b4c973b67b2a86c3a7cf" not in age_build
    assert "4d3d8417617e40b316d8b530e6065a69fe81bbc1" not in age_build
    assert (
        'test "$(git rev-parse HEAD:deploy/docker/ne-postgres-age)" = "$admitted_tree"'
        in logical_lines
    )
    assert 'DOCKER_HOST="$NE_AIRGAP_DOCKER_HOST"' in age_build
    assert 'NE_POSTGRES_AGE_SOURCE_REV="$admitted_tree"' in age_build
    assert "deploy/airgap/scripts/build-ne-postgres-age.sh" in age_build
    assert "--iidfile /mnt/ne-airgap/ne-postgres-age.iid" in age_build
    assert "ne-postgres-age:18.6-1.7.0" in age_build
    assert "linux/amd64" in age_build

    phase_a = next(s for s in _steps() if s.get("name") == _PHASE_A_STEP).get("run", "")
    for marker in ("CI=true", "GITHUB_ACTIONS=true", "NE_AIRGAP_CI=1"):
        assert marker in phase_a
    assert 'DOCKER_HOST="$NE_AIRGAP_DOCKER_HOST"' in phase_a
    assert 'NE_AIRGAP_DOCKER_HOST="$NE_AIRGAP_DOCKER_HOST"' in phase_a
    assert "--ci-age-iidfile /mnt/ne-airgap/ne-postgres-age.iid" in phase_a


def test_sealed_pilot_mounts_exact_skill_bundle_and_gates_api_readiness() -> None:
    pilot = yaml.safe_load(PILOT_COMPOSE_FILE.read_text(encoding="utf-8"))
    assert pilot["x-ne-app-env"]["NE_SKILLS_BUNDLE_DIR"] == "/etc/neutron/skills"
    for name in ("ne-api", "ne-worker"):
        assert "../../skills:/etc/neutron/skills:ro" in pilot["services"][name]["volumes"]
    probe = " ".join(pilot["services"]["ne-api"]["healthcheck"]["test"])
    assert "http://127.0.0.1:8000/healthz" in probe
    assert "ne.api" not in probe
    assert "skills_delivery" not in probe
    assert SKILL_FILE.is_file()


def test_every_uses_is_sha_pinned() -> None:
    for step in _steps():
        uses = step.get("uses")
        if uses is None:
            continue
        assert _SHA_PIN.match(uses), f"action not SHA-pinned: {uses}"


def test_checkout_and_setup_uv_shas_match_ci_yml() -> None:
    """Drift guard: this workflow's checkout/setup-uv pins must be byte-identical
    to ci.yml's — a divergent SHA here would mean a different, unreviewed
    action version silently entered the no-egress job."""
    with CI_WORKFLOW_FILE.open(encoding="utf-8") as fh:
        ci_data = yaml.safe_load(fh)
    ci_uses = {
        step["uses"] for job in ci_data["jobs"].values() for step in job["steps"] if "uses" in step
    }
    ci_checkout = next((u for u in ci_uses if u.startswith("actions/checkout@")), None)
    ci_setup_uv = next((u for u in ci_uses if u.startswith("astral-sh/setup-uv@")), None)
    assert ci_checkout is not None
    assert ci_setup_uv is not None

    our_uses = [step["uses"] for step in _steps() if "uses" in step]
    our_checkout = next((u for u in our_uses if u.startswith("actions/checkout@")), None)
    our_setup_uv = next((u for u in our_uses if u.startswith("astral-sh/setup-uv@")), None)
    assert our_checkout == ci_checkout, (our_checkout, ci_checkout)
    assert our_setup_uv == ci_setup_uv, (our_setup_uv, ci_setup_uv)


def test_executing_gate_used_for_every_phase() -> None:
    """Every phase test module must be driven through the shared executing-gate
    script (run-airgap-pytest.sh), never a bare `pytest`/`uv run pytest` call
    that could silently accept a skip."""
    run_texts = " ".join(step.get("run", "") for step in _steps())
    for phase_file in (
        "test_airgap_phase_a.py",
        "test_airgap_phase_b1.py",
        "test_airgap_phase_b2.py",
        "test_airgap_phase_c.py",
    ):
        pattern = f"run-airgap-pytest.sh deploy/airgap/tests/{phase_file}"
        assert pattern in run_texts, f"phase module not routed through executing gate: {phase_file}"


def test_run_airgap_pytest_script_has_skip_fail_gate() -> None:
    script = (SCRIPTS_DIR / "run-airgap-pytest.sh").read_text(encoding="utf-8")
    assert "passed" in script
    assert "skipped|no tests ran" in script


def test_teardown_is_always_and_diagnostics_on_failure() -> None:
    steps = _steps()
    teardown = next(s for s in steps if s.get("name") == "Teardown (always)")
    assert teardown.get("if") == "always()"
    diagnostics = next(s for s in steps if s.get("name") == "Diagnostics (on failure)")
    assert diagnostics.get("if") == "failure()"
    # Teardown must be the LAST non-diagnostic step (nothing meaningful runs after).
    names = _step_names()
    assert names.index("Teardown (always)") >= len(names) - 2


# ---------------------------------------------------------------------------
# (b2) NE-CI-NOEGRESS-VLLM-FLAKE — PR lane vs full lane split
# ---------------------------------------------------------------------------
def test_phase_c_gated_off_pull_request() -> None:
    """Pins the exact condition string. The flaky vLLM-CPU bring-up (Phase C)
    must be off the PR-blocking gate BY CONSTRUCTION, not by prose — a future
    edit that loosens or removes this condition goes red here first."""
    step = next(s for s in _steps() if s.get("name") == _PHASE_C_STEP)
    assert step.get("if") == "github.event_name != 'pull_request'"


def test_phase_c_pr_notice_step_present() -> None:
    """PRs must get an explicit, echoed deselection reason (mirrors the Phase
    B1 golden-vector deselect precedent) — never a silent skip that a
    reviewer could mistake for 'nothing to deselect'."""
    step = next(s for s in _steps() if s.get("name") == _PHASE_C_NOTICE_STEP)
    assert step.get("if") == "github.event_name == 'pull_request'"
    assert "::notice" in step.get("run", "")


def test_load_bearing_steps_are_unconditional() -> None:
    """The seal, all three prove-dead probes, Phase A (install/TLS/fail-closed
    container canary), B1, B2, and re-assert-seal must run in EVERY lane —
    this is the test-locked form of 'no seal assertion was weakened' that
    NE-CI-NOEGRESS-VLLM-FLAKE's PR body claims are checkable by reading the
    YAML. Conditionalizing any of these is the exact failure mode this unit
    is forbidden from introducing."""
    names = _step_names()
    steps_by_name = {s.get("name"): s for s in _steps()}
    for required in _ALWAYS_BLOCKING_STEPS:
        assert required in names, f"missing load-bearing step: {required}"
        assert "if" not in steps_by_name[required], (
            f"load-bearing step {required!r} must be unconditional (no 'if:'), "
            "it must run in every lane"
        )


def test_no_continue_on_error_anywhere() -> None:
    """Locks anti-pattern 2: the gate must never be made unable to fail, at
    either the job or the step level."""
    data = _load_workflow()
    job = data["jobs"]["no-egress"]
    assert "continue-on-error" not in job
    for step in _steps():
        assert "continue-on-error" not in step, (
            f"continue-on-error found on step {step.get('name')!r}"
        )


def test_phase_c_retry_budget_frozen_at_five() -> None:
    """Locks anti-pattern 1: raising the retry count is an explicitly
    rejected fix (NE-071c-FU already went 3->5 without curing the flake) — a
    future bump goes red here instead of silently lengthening the cycle."""
    step = next(s for s in _steps() if s.get("name") == _PHASE_C_STEP)
    run = step.get("run", "")
    # Parse the attempt list EXACTLY rather than substring-matching it. The
    # original assertion was `"for attempt in 1 2 3 4 5" in run`, which a bump
    # to `for attempt in 1 2 3 4 5 6` SATISFIES — the guard was defeatable by
    # precisely the edit it exists to forbid (orchestrator mutation, 5->6 left
    # all 34 tests green). Anchoring on the `; do` terminator and comparing the
    # parsed sequence makes the budget itself load-bearing.
    loop = re.search(r"^\s*for attempt in ([^;]+); do\s*$", run, re.MULTILINE)
    assert loop, f"expected a `for attempt in ...; do` bring-up retry loop in:\n{run}"
    assert loop.group(1).split() == ["1", "2", "3", "4", "5"], (
        "Phase C retry budget must stay frozen at 5 attempts — NE-071c-FU already "
        "went 3->5 without curing the flake, so raising it again is an explicitly "
        f"rejected fix. Found: {loop.group(1).split()!r}"
    )


def test_phase_c_classifier_never_gates() -> None:
    """Proves the known-signature grep added by NE-CI-NOEGRESS-VLLM-FLAKE is
    annotation-only: a real bring-up failure (known OR unknown signature)
    still exits 1, and the executing-gate invocation is never swallowed with
    `||` (anti-pattern 4)."""
    step = next(s for s in _steps() if s.get("name") == _PHASE_C_STEP)
    run = step.get("run", "")
    classifier_idx = run.find("determine_available_memory")
    assert classifier_idx != -1, "expected the known-signature classifier grep"
    assert "exit 1" in run[classifier_idx:], (
        "the classifier must fall through to the unchanged exit 1 — annotation only"
    )
    for line in run.splitlines():
        if "run-airgap-pytest.sh" in line:
            assert "||" not in line, (
                f"executing-gate invocation must never be swallowed with '||': {line!r}"
            )


def test_phase_c_classifier_signature_is_conjunctive_not_bare_enginecore() -> None:
    """Argus NE-CI-NOEGRESS-VLLM-FLAKE round-1 P1: a bare 'EngineCore'
    alternative ORed into the classifier grep matches almost any vLLM
    startup log (it's a generic per-line prefix, e.g. '(EngineCore_DP0
    pid=...)'), including a genuine config regression's failure output —
    which would misclassify a real regression as the tracked flake. The
    classifier line must instead require determine_available_memory AND
    (shm_broadcast OR 'RuntimeError: cancelled') together via `&&`, and must
    NOT contain a bare, disjunctively-ORed 'EngineCore' term."""
    step = next(s for s in _steps() if s.get("name") == _PHASE_C_STEP)
    run = step.get("run", "")
    classifier_line = next(
        line for line in run.splitlines() if "grep -q 'determine_available_memory'" in line
    )
    assert "&&" in classifier_line, (
        "the two grep halves must be conjunctive (&&), not merged into one OR-list"
    )
    narrowed = re.search(
        r"grep -qE ['\"]shm_broadcast\|RuntimeError: cancelled['\"]", classifier_line
    )
    assert narrowed, f"expected the narrowed shm_broadcast alternation: {classifier_line!r}"
    assert "|EngineCore" not in classifier_line and "EngineCore|" not in classifier_line, (
        "bare 'EngineCore' must not be ORed into the signature match — "
        "it is a generic log-line prefix, not evidence of the tracked flake"
    )


def test_only_phase_c_pair_carries_event_name_condition() -> None:
    """Argus NE-CI-NOEGRESS-VLLM-FLAKE round-1 P2: the README/commit claim
    that a PR breaking an image pin or the ne.serve download/verify path
    'still goes red on the PR gate' is only true if NOTHING besides the
    Phase C step and its PR-notice counterpart varies by event_name. Locks
    that by construction so a future edit conditionalizing any other step
    (e.g. the deterministic staging steps) goes red here immediately,
    regardless of how _ALWAYS_BLOCKING_STEPS is enumerated."""
    conditioned = {s.get("name") for s in _steps() if "github.event_name" in str(s.get("if", ""))}
    assert conditioned == {_PHASE_C_STEP, _PHASE_C_NOTICE_STEP}


# ---------------------------------------------------------------------------
# (c) overlay guardrails
# ---------------------------------------------------------------------------
def _load_yaml(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


_FORBIDDEN_OVERLAY_KEYS = ("image", "ports", "security_opt", "cap_drop")


def test_overlay_only_touches_services_present_in_base() -> None:
    overlay = _load_yaml(OVERLAY_FILE)
    # The overlay is applied on top of `pilot/compose.yaml -f airgap/compose.ci.yaml`,
    # and pilot `include`s the serve tier. Its legitimate scope is therefore ANY
    # real pilot-tier OR serve-tier service (e.g. vllm-cpu from serve, plus the
    # brain-profile tei-embed/tei-rerank from pilot). A YAML-level load does not
    # resolve `include`, so the base set is the union of both files' services.
    serve = _load_yaml(SERVE_COMPOSE_FILE)
    pilot = _load_yaml(PILOT_COMPOSE_FILE)
    base_services = set(serve["services"]) | set(pilot["services"])
    overlay_services = set(overlay.get("services", {}))
    assert overlay_services, "overlay defines no services"
    assert overlay_services <= base_services, (
        f"overlay defines services not present in the pilot/serve base tiers: "
        f"{overlay_services - base_services}"
    )


def test_overlay_never_touches_hardening_or_identity_keys() -> None:
    overlay = _load_yaml(OVERLAY_FILE)
    for name, cfg in overlay.get("services", {}).items():
        for key in _FORBIDDEN_OVERLAY_KEYS:
            assert key not in cfg, f"overlay service {name} illegally sets {key!r}"


def test_overlay_vllm_cpu_command_is_prefix_superset_of_base() -> None:
    overlay = _load_yaml(OVERLAY_FILE)
    serve = _load_yaml(SERVE_COMPOSE_FILE)
    base_command = serve["services"]["vllm-cpu"]["command"]
    overlay_command = overlay["services"]["vllm-cpu"]["command"]
    assert overlay_command[: len(base_command)] == base_command, (
        "overlay vllm-cpu command must keep the base command "
        "(--model/--served-model-name/xgrammar) as an exact prefix"
    )
    assert len(overlay_command) > len(base_command), (
        "overlay command must strictly extend the base command "
        "(e.g. --max-model-len), never merely replicate it"
    )


def test_pilot_compose_still_parses_and_is_untouched_by_this_unit() -> None:
    # Sanity: this unit reads the pilot tier but never edits it (mayTouch
    # discipline) — a parse failure here would mean an adjacent unit broke it,
    # not something this guardian fixes.
    data = _load_yaml(PILOT_COMPOSE_FILE)
    assert data["name"] == "ne-pilot"


# ---------------------------------------------------------------------------
# (d) canary image digest pin + provenance match
# ---------------------------------------------------------------------------
def test_canary_image_is_digest_pinned() -> None:
    script = (SCRIPTS_DIR / "canary.sh").read_text(encoding="utf-8")
    match = re.search(r'CANARY_IMAGE="([^"]+)"', script)
    assert match, "canary.sh must define CANARY_IMAGE"
    image = match.group(1)
    assert _DIGEST_PIN.search(image), f"canary image not digest-pinned: {image}"


def test_canary_digest_matches_readme_provenance() -> None:
    script = (SCRIPTS_DIR / "canary.sh").read_text(encoding="utf-8")
    match = re.search(r'CANARY_IMAGE="([^"]+)"', script)
    assert match
    digest_match = _DIGEST_PIN.search(match.group(1))
    assert digest_match
    digest = digest_match.group(0).removeprefix("@")
    readme = README_FILE.read_text(encoding="utf-8")
    assert digest in readme, (
        f"canary.sh digest {digest} not found in deploy/airgap/README.md provenance table"
    )


# ---------------------------------------------------------------------------
# (e) license headers on every deploy/airgap py/sh file
# ---------------------------------------------------------------------------
def _has_header(path: Path) -> bool:
    seen_copyright = False
    seen_owner = False
    with path.open(encoding="utf-8") as handle:
        for _, line in zip(range(_HEADER_SCAN_LINES), handle, strict=False):
            if _COPYRIGHT_MARKER in line:
                seen_copyright = True
            if _OWNER_MARKER in line:
                seen_owner = True
    return seen_copyright and seen_owner


def test_every_airgap_source_file_has_license_header() -> None:
    files = sorted(AIRGAP_DIR.rglob("*.py")) + sorted(AIRGAP_DIR.rglob("*.sh"))
    assert files, "expected .py/.sh files under deploy/airgap"
    missing = [f for f in files if not _has_header(f)]
    assert not missing, f"missing source header: {missing}"


# ---------------------------------------------------------------------------
# (f) every phase module gates on NE_AIRGAP
# ---------------------------------------------------------------------------
def test_every_phase_module_has_ne_airgap_gate() -> None:
    phase_modules = sorted(TESTS_DIR.glob("test_airgap_phase_*.py"))
    assert phase_modules, "expected at least one test_airgap_phase_*.py module"
    for module in phase_modules:
        text = module.read_text(encoding="utf-8")
        assert "AIRGAP_ACTIVE" in text, f"{module.name} missing the NE_AIRGAP module gate"
        assert "allow_module_level=True" in text, f"{module.name} gate must skip at module level"


# ---------------------------------------------------------------------------
# (g) inner-dockerd cgroup-v2 launch recipe (regression pin)
# ---------------------------------------------------------------------------
def test_inner_dockerd_helper_remounts_cgroup2() -> None:
    text = (SCRIPTS_DIR / "netns-inner-dockerd.sh").read_text(encoding="utf-8")
    assert "mount -t cgroup2" in text
    assert "cgroup2fs" in text
    assert "native.cgroupdriver=cgroupfs" in text
    assert "--cgroup-parent /ne-airgap" in text
    assert "exec dockerd" in text


def test_stage_delegates_dockerd_startup_to_helper() -> None:
    text = (SCRIPTS_DIR / "netns-stage.sh").read_text(encoding="utf-8")
    assert "netns-inner-dockerd.sh" in text
    assert 'exec "${NS}" dockerd' not in text


def test_inner_dockerd_runs_dedicated_containerd() -> None:
    """Regression pin for FIX 2: the inner dockerd must launch its OWN
    containerd inside the namespace and be pointed at it via --containerd.
    Without this it attaches to the host's shared containerd, running every
    task in the host mount+network namespaces (empty-rootfs crash AND a
    silently-bypassed egress seal)."""
    text = (SCRIPTS_DIR / "netns-inner-dockerd.sh").read_text(encoding="utf-8")
    assert "containerd --config" in text, "launcher must start a dedicated containerd"
    assert "--containerd" in text, "dockerd must be pointed at the dedicated containerd socket"
    # fail-loud, never fall back to the host containerd
    assert "command -v containerd" in text
    # the dedicated containerd's socket is private, under STATE_DIR
    assert "containerd.sock" in text


def test_stage_readiness_gate_has_container_run_smoke() -> None:
    """The stage readiness gate must prove a container can actually RUN (not
    merely that `docker info` answers), while egress is still up, so a
    can't-run-containers regression fails loud at staging instead of masquerading
    as an egress-dead result at the post-seal canary."""
    text = (SCRIPTS_DIR / "netns-stage.sh").read_text(encoding="utf-8")
    assert "hello-world@sha256:" in text, "stage smoke image must be digest-pinned"
    assert "run --rm" in text, "stage must actually RUN a throwaway container"


def test_stage_smoke_digest_matches_readme_provenance() -> None:
    text = (SCRIPTS_DIR / "netns-stage.sh").read_text(encoding="utf-8")
    match = re.search(r'SMOKE_IMAGE="([^"]+)"', text)
    assert match, "netns-stage.sh must define SMOKE_IMAGE"
    digest_match = _DIGEST_PIN.search(match.group(1))
    assert digest_match, f"stage smoke image not digest-pinned: {match.group(1)}"
    digest = digest_match.group(0).removeprefix("@")
    readme = README_FILE.read_text(encoding="utf-8")
    assert digest in readme, (
        f"netns-stage.sh smoke digest {digest} not in deploy/airgap/README.md provenance table"
    )


def test_netns_down_reaps_dedicated_containerd() -> None:
    text = (SCRIPTS_DIR / "netns-down.sh").read_text(encoding="utf-8")
    assert "containerd.pid" in text, "teardown must reap the dedicated inner containerd by pidfile"


def test_canary_container_probe_has_image_precheck() -> None:
    text = (SCRIPTS_DIR / "canary.sh").read_text(encoding="utf-8")
    assert "image inspect" in text
    assert text.index("image inspect") < text.index('docker -H "${INNER_DOCKER_HOST}" run')


# --- PRE-009A (A3): air-gap surfaces carry NAMES, never key material ----------

_PRIVATE_PAYLOAD_ENVS = ("NE_AGENT_PRIVATE_PAYLOAD_KEY_ID", "NE_AGENT_PRIVATE_PAYLOAD_KEK_B64")


def test_ci_bundle_seeds_canonical_256_bit_runtime_keys() -> None:
    """The real air-gap fixture must seed values accepted by both key loaders."""
    payloads = build_ci_secret_payloads(REQUIRED_SECRETS)
    for env_name in (
        "NE_AGENT_PRIVATE_PAYLOAD_KEK_B64",
        "NE_SOURCE_AUTHORITY_CONTINUATION_HMAC_KEY_B64",
    ):
        spec = REQUIRED_SECRETS[env_name]
        encoded = payloads[spec["path"]][spec["field"]]
        assert "=" not in encoded
        assert len(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))) == 32


def test_sealed_pilot_shares_one_null_valued_private_payload_key_reference() -> None:
    """Both roots take the KEK from the SAME anchor entry, and it is null-valued.

    A per-service copy is exactly how ne-api and ne-worker come to hold different
    keys; a literal value would put key material into a tracked file.
    """
    pilot = yaml.safe_load(PILOT_COMPOSE_FILE.read_text(encoding="utf-8"))
    anchor = pilot["x-ne-app-env"]
    for name in _PRIVATE_PAYLOAD_ENVS:
        assert name in anchor, name
        assert anchor[name] is None, name
    for service in ("ne-api", "ne-worker"):
        env = pilot["services"][service]["environment"]
        assert {key: env[key] for key in _PRIVATE_PAYLOAD_ENVS} == dict.fromkeys(
            _PRIVATE_PAYLOAD_ENVS
        )


def test_the_zarf_package_declares_no_private_payload_key_material() -> None:
    """The KEK reaches an air-gapped install through the bundle SecretStore, never
    through a Zarf variable, constant or package template."""
    zarf = yaml.safe_load(ZARF_FILE.read_text(encoding="utf-8"))
    declared = {item["name"] for item in zarf.get("variables", [])}
    declared |= {item["name"] for item in zarf.get("constants", [])}
    for name in _PRIVATE_PAYLOAD_ENVS:
        assert name not in declared, name
    # Positive control: the package DOES declare the variables it is supposed to,
    # so the absence above is a real exclusion and not an empty parse.
    assert "NE_HOSTNAME" in declared
    assert "NE_CONFIG_HASH" in declared


# --- 015A0-AUTHORITY-CORE: the continuation key is a NAME on every air-gap surface


_CONTINUATION_ENVS = (
    "NE_SOURCE_AUTHORITY_CONTINUATION_HMAC_KEY_ID",
    "NE_SOURCE_AUTHORITY_CONTINUATION_HMAC_KEY_B64",
)


def test_sealed_pilot_shares_one_null_valued_continuation_key_reference() -> None:
    """Both roots take the continuation key from the SAME anchor entry, null-valued.

    Two roots holding different continuation keys do not fail loudly: each simply
    refuses the other's outstanding tokens, which reads as an intermittent paging
    bug rather than as a split key. A per-service copy of this block is exactly
    how that happens.
    """
    pilot = yaml.safe_load(PILOT_COMPOSE_FILE.read_text(encoding="utf-8"))
    anchor = pilot["x-ne-app-env"]
    for name in _CONTINUATION_ENVS:
        assert name in anchor, name
        assert anchor[name] is None, name
    for service in ("ne-api", "ne-worker"):
        env = pilot["services"][service]["environment"]
        assert {key: env[key] for key in _CONTINUATION_ENVS} == dict.fromkeys(_CONTINUATION_ENVS)


def test_the_zarf_package_declares_no_continuation_key_material() -> None:
    """The continuation key reaches an air-gapped install through the bundle
    SecretStore, never through a Zarf variable, constant or package template."""
    zarf = yaml.safe_load(ZARF_FILE.read_text(encoding="utf-8"))
    declared = {item["name"] for item in zarf.get("variables", [])}
    declared |= {item["name"] for item in zarf.get("constants", [])}
    for name in _CONTINUATION_ENVS:
        assert name not in declared, name
    # Positive control: the package DOES declare the variables it is supposed to,
    # so the absence above is a real exclusion and not an empty parse.
    assert "NE_HOSTNAME" in declared
    assert "NE_CONFIG_HASH" in declared


# --- 015A0-AUTHORITY-M365: the sub-SLA sweep is one shared, non-secret plan


_SWEEP_ENVS = {
    "NE_M365_AUTHORITY_ENABLED": "false",
    "NE_M365_AUTHORITY_SWEEP_CADENCE_SECONDS": "120",
    "NE_M365_AUTHORITY_SWEEP_BATCH_SIZE": "50",
    "NE_M365_AUTHORITY_SWEEP_MAX_BATCH_DURATION_SECONDS": "60",
    "NE_M365_AUTHORITY_SWEEP_MAX_POINTS_PER_SCOPE": "500",
}


def test_sealed_pilot_gives_both_roots_the_identical_sweep_plan() -> None:
    """A per-root sweep value is how the API and worker come to disagree.

    The API projects the same contract descriptor the worker admits under, so the
    sweep plan is a SHARED anchor entry with real values — unlike the key
    material above, none of it is secret and a null value would leave a
    deployment unable to state whether a quiet item is still fresh.
    """
    pilot = yaml.safe_load(PILOT_COMPOSE_FILE.read_text(encoding="utf-8"))
    anchor = pilot["x-ne-app-env"]
    assert {name: anchor[name] for name in _SWEEP_ENVS} == _SWEEP_ENVS
    for service in ("ne-api", "ne-worker"):
        env = pilot["services"][service]["environment"]
        assert {name: env[name] for name in _SWEEP_ENVS} == _SWEEP_ENVS


def test_the_sealed_sweep_plan_completes_a_rotation_inside_the_connector_sla() -> None:
    """The rendered numbers must satisfy the rule, not merely be present.

    ``ceil(max_points / batch_size) * (cadence + max_batch_duration) < SLA`` is
    checked against the connector's own default PT1H, using the SAME plan object
    the producer composes with — so a future edit that renders an infeasible
    plan fails here rather than at a customer's first quiet sweep.
    """
    from decimal import Decimal

    from ne.connect.authority_admission import SweepCadencePlan

    pilot = yaml.safe_load(PILOT_COMPOSE_FILE.read_text(encoding="utf-8"))
    anchor = pilot["x-ne-app-env"]
    plan = SweepCadencePlan(
        cadence_seconds=Decimal(str(anchor["NE_M365_AUTHORITY_SWEEP_CADENCE_SECONDS"])),
        batch_size=int(anchor["NE_M365_AUTHORITY_SWEEP_BATCH_SIZE"]),
        max_batch_duration_seconds=Decimal(
            str(anchor["NE_M365_AUTHORITY_SWEEP_MAX_BATCH_DURATION_SECONDS"])
        ),
        max_points_per_scope=int(anchor["NE_M365_AUTHORITY_SWEEP_MAX_POINTS_PER_SCOPE"]),
    )
    plan.require_sub_sla(Decimal(3600))


def test_the_zarf_package_declares_no_m365_sweep_variable() -> None:
    """The sweep plan is compose/Helm configuration, not a Zarf install variable."""
    zarf = yaml.safe_load(ZARF_FILE.read_text(encoding="utf-8"))
    declared = {item["name"] for item in zarf.get("variables", [])}
    declared |= {item["name"] for item in zarf.get("constants", [])}
    for name in _SWEEP_ENVS:
        assert name not in declared, name
    assert "NE_CONFIG_HASH" in declared
