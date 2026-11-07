# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[3]
_IMAGE = "ne-postgres-age:18.6-1.7.0"
_RUNTIME_BASE = (
    "cgr.dev/chainguard/postgres:latest@"
    "sha256:4401cf2919e749cd77b634a72e4a677c1a1afc12baddb21faf28df326162472e"
)
_BUILDER_BASE = (
    "cgr.dev/chainguard/postgres:latest-dev@"
    "sha256:3f12033ccd330632fccfc85435fa7a3f5498132c40127bec07a612322605077f"
)
_AGE_SHA512 = (
    "129e64d70ec82e0fb47202145af45915b335048472c9a869129c31e4cb59f15a"
    "271a929a36a7abb2822272591e6476786c36908cb35a10cd73f2ecd0a617d3bb"
)
_AGE_ASC_SHA512 = (
    "5bc1b1eb000ab85d2f9d567591f8a65a57ee503caff77b6b5a06a603c2e6c60e"
    "70420fab4c803ed23848f7fb0084016f03e0075fb4a9a43fb90be7d45c721e2a"
)
_AGE_KEYS_SHA512 = (
    "0a79d404d3d00d55d4dcf7b2e010310884acf194e57e37b67c3cb0d79678c5d2"
    "dc68ec482c522ed9946f4a3cd4335c936cd7176ead799b30e602741fb115e1f3"
)
_AGE_SIGNER = "429306038E35AC054DBB4B5826B6CD9DCD5B0045"
_BUILDER_LOCK = _ROOT / "deploy/docker/ne-postgres-age/builder-apk-packages.lock"
_BUILDER_LOCK_SHA256 = "82c2eeaf1f041a91b326409f46b87f093ec40daf50a697746b71749aa78fd42b"
_BUILDER_CONTENT_SHA256 = "3a0d62a4bec82b2059400fa083d5f3e6ff6702bf111651bed8a719b1a7c13cb4"
_RUNTIME_APK_SHA256 = "ffe2de953ce4575587c9a4083b3af4a7c6832af27248a2ca96ef0e3b8dd6949a"
_LICENSES_SHA256 = "5760b43b0a285c7bf98b9dba4a7bef3bfb35df9c545e564e8c56ed4790fc59da"


def test_dockerfile_is_exact_pg186_age170_and_minimal_runtime() -> None:
    text = (_ROOT / "deploy/docker/ne-postgres-age/Dockerfile").read_text(encoding="utf-8")
    lock_bytes = _BUILDER_LOCK.read_bytes()
    lock = lock_bytes.decode("utf-8").splitlines()
    from_images = re.findall(r"^FROM ([^ ]+)", text, re.MULTILINE)

    assert from_images == [_RUNTIME_BASE, "scratch", _BUILDER_BASE, "runtime-base"]
    assert "ARG AGE_" not in text
    assert "FROM scratch AS age-src" in text
    assert "--mount=type=bind,from=age-src,target=/tmp/age-prefetched" in text
    assert "sh /tmp/fetch-age-src.sh /tmp/age-download /tmp/age-prefetched" in text
    assert _AGE_SHA512 in text
    assert _AGE_ASC_SHA512 in text
    # Whole-line equality, not containment: the KEYS hash is checked by a
    # single `sha512sum -c -` line, and a second `echo`-ed hash (or a hash that
    # merely appears in a comment) must not satisfy this guard.
    assert f'    && echo "{_AGE_KEYS_SHA512}  /tmp/age-KEYS" \\' in text.splitlines()
    assert f"[GNUPG:] VALIDSIG {_AGE_SIGNER} " in text
    # The base.digest LABEL is the only place the runtime digest is restated
    # outside the FROM line; pinned by exact list equality so a stale digest
    # (or a second LABEL) fails rather than being tolerated by containment.
    assert re.findall(r'org\.opencontainers\.image\.base\.digest="([^"]*)"', text) == [
        _RUNTIME_BASE.rsplit("@", 1)[1]
    ]
    for package_pin in (
        "bison=3.8.2-r64",
        "build-base=1-r10",
        "flex=2.6.4-r16",
        "gpg=2.4.9-r12",
        "gpgv=2.4.9-r12",
        "perl=5.44.0-r4",
        "postgresql-18-dev=18.6-r7",
    ):
        assert package_pin in lock
    assert len(lock) == 112
    assert lock == sorted(set(lock))
    assert all(re.fullmatch(r"[A-Za-z0-9+_.-]+=[A-Za-z0-9+_.-]+", item) for item in lock)
    assert hashlib.sha256(lock_bytes).hexdigest() == _BUILDER_LOCK_SHA256
    assert text.count("PG_CONFIG=/usr/bin/pg_config") == 2
    assert "COPY --from=age-builder /age-artifacts/lib/age.so" in text
    assert "COPY --from=age-builder /age-artifacts/extension/" in text
    assert "su-exec" not in text
    assert "--allow-untrusted" not in text
    assert "apk fetch" not in text
    assert _BUILDER_LOCK_SHA256 in text
    assert _BUILDER_CONTENT_SHA256 in text
    assert 'test "$(wc -l < /tmp/builder-apk-packages.lock)" -eq 112' in text
    assert 'test "$(wc -l < /tmp/builder-apk-packages.txt)" -eq 112' in text
    assert "cmp /tmp/builder-apk-packages.lock /tmp/builder-apk-packages.txt" in text
    assert _RUNTIME_APK_SHA256 in text
    assert 'test "$(wc -l < /age-artifacts/runtime-apk-inventory.tsv)" -eq 62' in text
    assert _LICENSES_SHA256 in text
    assert "COPY deploy/docker/ne-postgres-age/THIRD_PARTY_LICENSES.txt" in text

    runtime = text.split("FROM runtime-base AS runtime", maxsplit=1)[1]
    assert "COPY --from=age-builder /age-artifacts/lib/age.so" in runtime
    assert 'ENTRYPOINT ["/usr/bin/docker-entrypoint.sh", "postgres"]' in runtime
    assert "apk add" not in runtime


def test_apache_downloads_have_exact_bounded_retries_and_timeouts() -> None:
    script = (_ROOT / "deploy/docker/ne-postgres-age/fetch-age-src.sh").read_text(encoding="utf-8")
    logical = " ".join(script.replace("\\\n", " ").split())

    # One wget invocation, bounded per attempt, inside a bounded mirror loop.
    assert logical.count("wget ") == 1
    assert (
        "wget -q --tries=2 --timeout=30 --retry-connrefused "
        "--retry-on-http-error=500,502,503,504 --waitretry=2 "
        '-O "${out}" "${mirror}/${rel}"'
    ) in logical
    assert (
        'mirrors="https://dlcdn.apache.org https://downloads.apache.org '
        'https://archive.apache.org/dist"'
    ) in script
    assert "rounds=5" in script
    assert "delay=$((delay * 2))" in script
    assert 'age_path="age/PG18/1.7.0"' in script
    # Every file is pinned; a mismatching download is discarded, never used.
    for name, rel, digest in (
        ("age-KEYS", '"age/KEYS"', _AGE_KEYS_SHA512),
        (
            "apache-age-1.7.0-src.tar.gz",
            '"${age_path}/apache-age-1.7.0-src.tar.gz"',
            _AGE_SHA512,
        ),
        (
            "apache-age-1.7.0-src.tar.gz.asc",
            '"${age_path}/apache-age-1.7.0-src.tar.gz.asc"',
            _AGE_ASC_SHA512,
        ),
    ):
        assert f"fetch {name} {rel} {digest}" in logical
    assert "SHA-512 mismatch" in script
    assert "--no-check-certificate" not in script
    assert "set -eu" in script


def test_provenance_and_license_record_bind_exact_upstreams() -> None:
    readme = (_ROOT / "deploy/docker/ne-postgres-age/README.md").read_text(encoding="utf-8")
    licenses = (_ROOT / "deploy/docker/ne-postgres-age/THIRD_PARTY_LICENSES.txt").read_text(
        encoding="utf-8"
    )

    assert _RUNTIME_BASE in readme
    assert _BUILDER_BASE in readme
    assert _AGE_SHA512 in readme
    # Whole-row equality for the mutable-upstream KEYS pin: the README row and
    # the Dockerfile literal must carry the same bytes, so a re-pin that lands
    # in only one of the two sites fails here.
    assert f"| Apache AGE KEYS SHA-512 | `{_AGE_KEYS_SHA512}` |" in readme.splitlines()
    assert "429306038E35AC054DBB4B5826B6CD9DCD5B0045" in readme
    assert "PostgreSQL License" in licenses
    assert "Apache License" in licenses
    assert "Version 2.0, January 2004" in licenses
    assert "Chainguard PostgreSQL / Wolfi base" in licenses
    assert "https://github.com/wolfi-dev/os" in licenses
    assert _BUILDER_LOCK_SHA256 in readme
    assert _BUILDER_CONTENT_SHA256 in readme
    assert _RUNTIME_APK_SHA256 in readme
    assert "tools/supply-chain/vex/ne-postgres-age.openvex.json" in readme


def test_build_script_uses_one_local_tag_and_source_revision_label() -> None:
    text = (_ROOT / "deploy/airgap/scripts/build-ne-postgres-age.sh").read_text(encoding="utf-8")

    assert f'image_tag="{_IMAGE}"' in text
    assert "deploy/docker/ne-postgres-age/Dockerfile" in text
    assert "org.neutron.ne-postgres-age.source-rev" in text
    assert "must be the exact lowercase 40-hex source commit" in text
    assert 'if [[ ! "${source_revision}" =~ ^[0-9a-f]{40}$ ]]' in text
    assert 'if [[ -n "${iidfile_path:-}" ]]' in text
    assert "DOCKER_BUILDKIT=1 docker build" in text
    assert "--platform linux/amd64" in text
    assert 'docker image inspect "${image_tag}"' in text
    assert 'build_args+=(--build-context "age-src=${age_src_dir}")' in text


_TRIVY_REPORT_STEP = "Trivy PostgreSQL + AGE image full report (JSON, un-VEX, all findings)"
_RAW_GRYPE_STEP = "Grype PostgreSQL + AGE image raw findings (JSON, un-VEX)"
_EXPORT_STEP = "Export PostgreSQL + AGE image filesystem (created, never started)"
_GATE_STEP = "Image VEX gate (reachability probe, ledger exactness, fix check; fail-closed)"
_TRIVY_STEP = "Trivy PostgreSQL + AGE image (CRITICAL/HIGH, fail-closed)"
_GRYPE_STEP = "Grype PostgreSQL + AGE image (fail on high)"
_IMAGE_VEX = "tools/supply-chain/vex/ne-postgres-age.openvex.json"
_IMAGE_REACHABILITY = "tools/supply-chain/vex/ne-postgres-age.reachability.json"
_REPORT_STEP = "Vulnerability report (report-only, includes unfixed findings)"
_REPORT_UPLOAD_STEP = "Upload full vulnerability reports (report-only, includes unfixed findings)"


def test_required_vulnerability_job_builds_smokes_gates_and_scans_image() -> None:
    workflow = yaml.safe_load(
        (_ROOT / ".github/workflows/supply-chain.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["vuln-scan"]["steps"]
    by_name = {step.get("name"): step.get("run", "") for step in steps}
    step_by_name = {step.get("name"): step for step in steps}

    verify = by_name["Verify signed Chainguard PostgreSQL base manifests"]
    assert _RUNTIME_BASE in verify
    assert _BUILDER_BASE in verify
    assert "cosign verify" in verify
    assert "https://token.actions.githubusercontent.com" in verify
    assert (
        "https://github.com/chainguard-images/images/.github/workflows/"
        "release.yaml@refs/heads/main" in verify
    )

    build = by_name["Build pinned PostgreSQL 18.6 + Apache AGE 1.7.0 image"]
    assert 'NE_POSTGRES_AGE_SOURCE_REV="$GITHUB_SHA"' in build
    assert "bash deploy/airgap/scripts/build-ne-postgres-age.sh" in build
    assert '--iidfile "$RUNNER_TEMP/ne-postgres-age.iid"' in build
    assert 'docker image inspect --format="{{.Id}}"' in build
    assert (
        "--format='{{ index .Config.Labels \"org.neutron.ne-postgres-age.source-rev\" }}'" in build
    )
    assert '\\"org.neutron.ne-postgres-age.source-rev\\"' not in build
    assert by_name["Live PostgreSQL 18.6 + AGE extension, GIN, and readback smoke"] == (
        "NE_POSTGRES_AGE_LIVE=1 NE_POSTGRES_AGE_LIVE_REQUIRED=1 "
        "uv run pytest tools/ne-cli/tests/test_ne_postgres_age_live.py -q"
    )
    # The five image-scan/gate steps are pinned by EQUALITY, not containment: a
    # containment assert cannot forbid an added flag, and `--vex` moving onto
    # the Trivy scan (or off the Grype one) is exactly the change that must
    # fail here. C11: no threshold moves, no scanner is disabled, and the Trivy
    # image scan never reads a VEX ledger. Gate policy: fail only on HIGH/CRITICAL
    # findings that HAVE a fix (`--ignore-unfixed` / `--only-fixed`); unfixed ones
    # are report-only and land in the job summary and the vuln-reports artifact.
    trivy = by_name[_TRIVY_STEP]
    raw_grype = by_name[_RAW_GRYPE_STEP]
    export = by_name[_EXPORT_STEP]
    image_gate = by_name[_GATE_STEP]
    grype = by_name[_GRYPE_STEP]
    trivy_report = by_name[_TRIVY_REPORT_STEP]
    assert trivy_report == (
        'image_id="$(cat "$RUNNER_TEMP/ne-postgres-age.iid")"\n'
        "trivy image --scanners vuln --format json \\\n"
        '  --output "$RUNNER_TEMP/ne-postgres-age-trivy-raw.json" "$image_id"\n'
    )
    assert trivy == (
        'image_id="$(cat "$RUNNER_TEMP/ne-postgres-age.iid")"\n'
        "trivy image --scanners vuln --exit-code 1 --severity CRITICAL,HIGH --ignore-unfixed "
        '"$image_id"\n'
    )
    assert raw_grype == (
        'image_id="$(cat "$RUNNER_TEMP/ne-postgres-age.iid")"\n'
        'grype "$image_id" -o json --file "$RUNNER_TEMP/ne-postgres-age-grype-raw.json"\n'
    )
    assert export == (
        'image_id="$(cat "$RUNNER_TEMP/ne-postgres-age.iid")"\n'
        'probe_container="$(docker create "$image_id")"\n'
        'docker export "$probe_container" -o "$RUNNER_TEMP/ne-postgres-age.export.tar"\n'
        'docker rm "$probe_container"\n'
    )
    assert image_gate == (
        "python3 tools/supply-chain/image_vex_gate.py \\\n"
        '  --export "$RUNNER_TEMP/ne-postgres-age.export.tar" \\\n'
        '  --grype "$RUNNER_TEMP/ne-postgres-age-grype-raw.json" \\\n'
        f"  --vex {_IMAGE_VEX} \\\n"
        f"  --reachability {_IMAGE_REACHABILITY}\n"
        'rm -f "$RUNNER_TEMP/ne-postgres-age.export.tar"\n'
    )
    assert grype == (
        'image_id="$(cat "$RUNNER_TEMP/ne-postgres-age.iid")"\n'
        f'grype "$image_id" --fail-on high --only-fixed --vex {_IMAGE_VEX}\n'
    )
    assert "--vex" not in trivy
    assert "--vex" not in trivy_report

    image_steps = (
        _TRIVY_REPORT_STEP,
        _TRIVY_STEP,
        _RAW_GRYPE_STEP,
        _EXPORT_STEP,
        _GATE_STEP,
        _GRYPE_STEP,
    )
    names = [step.get("name") for step in steps]
    for name in image_steps:
        # Only {name, run}: rejects `if:`, `continue-on-error:`, `env:`, `shell:`
        # — a conditional or soft-failing gate is a deleted gate.
        assert set(step_by_name[name]) == {"name", "run"}, name
        assert names.count(name) == 1, name
        assert "vex/openvex.json" not in by_name[name], name
    # The image ledger is read by exactly the gate and the VEX'd Grype scan; the
    # reachability map by exactly the gate. The dir-scan ledger never enters an
    # image step (asserted just above), and neither does the image ledger leave
    # these two.
    assert [name for name in names if _IMAGE_VEX in by_name.get(name, "")] == [
        _GATE_STEP,
        _GRYPE_STEP,
    ]
    assert [name for name in names if _IMAGE_REACHABILITY in by_name.get(name, "")] == [_GATE_STEP]

    sbom = by_name["Generate final PostgreSQL + AGE image CycloneDX SBOM"]
    assert "trivy image --format cyclonedx" in sbom
    assert '"$image_id"' in sbom
    assert "runtime-apk-inventory.tsv" in sbom
    assert "AGPL|SSPL|BUSL|FSL|NONCOMMERCIAL" in sbom
    assert '"name": "PostgreSQL"' in sbom
    assert '"name": "Apache AGE"' in sbom
    assert 'docker save "$image_id" | gzip -n -9' in sbom
    assert '--subject "$RUNNER_TEMP/ne-postgres-age.cdx.json"' in sbom
    assert '--subject "$RUNNER_TEMP/ne-postgres-age.oci.tar.gz"' in sbom
    upload = next(
        step
        for step in steps
        if step.get("name") == "Upload final PostgreSQL + AGE image artifact, SBOM, and provenance"
    )
    assert upload["uses"] == "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
    assert upload["with"]["if-no-files-found"] == "error"
    assert upload["with"]["path"].splitlines() == [
        "${{ runner.temp }}/ne-postgres-age.cdx.json",
        "${{ runner.temp }}/ne-postgres-age.oci.tar.gz",
        "${{ runner.temp }}/ne-postgres-age.intoto.json",
    ]
    assert "uv run --package ne-cli ne release provenance" in sbom

    build_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Build pinned PostgreSQL 18.6 + Apache AGE 1.7.0 image"
    )
    smoke_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Live PostgreSQL 18.6 + AGE extension, GIN, and readback smoke"
    )
    trivy_report_index = names.index(_TRIVY_REPORT_STEP)
    trivy_index = names.index(_TRIVY_STEP)
    raw_index = names.index(_RAW_GRYPE_STEP)
    export_index = names.index(_EXPORT_STEP)
    gate_index = names.index(_GATE_STEP)
    grype_index = names.index(_GRYPE_STEP)
    sbom_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Generate final PostgreSQL + AGE image CycloneDX SBOM"
    )
    upload_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Upload final PostgreSQL + AGE image artifact, SBOM, and provenance"
    )
    # The gate must run BEFORE the VEX'd scan: a gate that runs after it can no
    # longer stop a dishonest statement from having already suppressed a finding.
    # The full raw reports run before any failing image scan so the report-only
    # pass always has them.
    assert (
        build_index
        < smoke_index
        < trivy_report_index
        < raw_index
        < trivy_index
        < export_index
        < gate_index
        < grype_index
        < sbom_index
        < upload_index
    )


def test_report_only_pass_publishes_unfixed_findings_and_never_gates() -> None:
    workflow = yaml.safe_load(
        (_ROOT / ".github/workflows/supply-chain.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["vuln-scan"]["steps"]
    names = [step.get("name") for step in steps]
    step_by_name = {step.get("name"): step for step in steps}
    report = step_by_name[_REPORT_STEP]
    upload = step_by_name[_REPORT_UPLOAD_STEP]
    # Runs even when a gate failed, and is the last thing the job does.
    assert report["if"] == "always()"
    assert upload["if"] == "always()"
    assert names[-2:] == [_REPORT_STEP, _REPORT_UPLOAD_STEP]
    assert "continue-on-error" not in report and "continue-on-error" not in upload
    for raw in (
        "ne-postgres-age-trivy-raw.json",
        "ne-postgres-age-grype-raw.json",
        "trivy-raw.json",
        "grype-raw.json",
    ):
        assert raw in report["run"], raw
        assert f"${{{{ runner.temp }}}}/{raw}" in upload["with"]["path"].splitlines(), raw
    assert '--out "$GITHUB_STEP_SUMMARY"' in report["run"]
    assert upload["uses"] == "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
    assert upload["with"]["name"] == "vuln-reports"
    # The report reads raw scanner output only: never a VEX ledger.
    assert "vex/" not in report["run"]
    # The raw repo reports are unfiltered (no severity cut) and precede the gates.
    runs = {step.get("name"): step.get("run", "") for step in steps}
    raw_trivy = runs["Trivy raw findings (JSON, un-VEX, all findings)"]
    assert "--severity" not in raw_trivy and "--ignore-unfixed" not in raw_trivy
    assert "--only-fixed" not in runs["Grype raw findings (JSON, un-VEX, all findings)"]
    assert names.index("Trivy raw findings (JSON, un-VEX, all findings)") < names.index(
        "Trivy (vuln, CRITICAL/HIGH with a fix, fail-closed)"
    )
    assert names.index("Grype raw findings (JSON, un-VEX, all findings)") < names.index(
        "Grype (fail on high with a fix)"
    )
    assert "--ignore-unfixed" in runs["Trivy (vuln, CRITICAL/HIGH with a fix, fail-closed)"]
    assert "--only-fixed" in runs["Grype (fail on high with a fix)"]
