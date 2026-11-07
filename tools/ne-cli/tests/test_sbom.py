# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for the bespoke stdlib CycloneDX 1.6 SBOM generator (`ne release sbom`)."""

from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path

from click.testing import CliRunner

from ne.cli import main
from ne.cli.licenses import Policy, default_policy_path
from ne.cli.sbom import _is_spdx_expression, build_sbom

_UUID4_URN = re.compile(
    r"^urn:uuid:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

REPO_ROOT = Path(__file__).resolve().parents[3]
LOCK = REPO_ROOT / "uv.lock"


def _lock() -> dict:
    return tomllib.loads(LOCK.read_text(encoding="utf-8"))


def _lock_pkg(name: str) -> dict:
    return next(p for p in _lock()["package"] if p["name"] == name)


def _policy() -> Policy:
    return Policy.load(default_policy_path(REPO_ROOT))


def _is_member(pkg: dict) -> bool:
    src = pkg.get("source", {})
    return "editable" in src or "directory" in src


def _is_first_party(pkg: dict) -> bool:
    src = pkg.get("source", {})
    return any(k in src for k in ("editable", "virtual", "directory"))


def _click_sha256s() -> set[str]:
    pkg = _lock_pkg("click")
    hashes = [pkg["sdist"]["hash"]] + [w["hash"] for w in pkg.get("wheels", [])]
    return {h[len("sha256:") :] for h in hashes if h.startswith("sha256:")}


def test_bom_format_and_spec_version() -> None:
    doc = build_sbom(LOCK, _policy(), "ne-cli")
    assert doc["bomFormat"] == "CycloneDX"
    assert doc["specVersion"] == "1.6"
    assert doc["version"] == 1
    assert doc["$schema"] == "http://cyclonedx.org/schema/bom-1.6.schema.json"
    assert _UUID4_URN.match(doc["serialNumber"])


def test_components_include_click_pinned_with_license_and_hashes() -> None:
    doc = build_sbom(LOCK, _policy(), "ne-cli")
    click_comp = next(c for c in doc["components"] if c["name"] == "click")
    version = _lock_pkg("click")["version"]
    assert click_comp["version"] == version
    assert click_comp["licenses"] == [{"expression": "BSD-3-Clause"}]
    assert all(h["alg"] == "SHA-256" for h in click_comp["hashes"])
    assert {h["content"] for h in click_comp["hashes"]} == _click_sha256s()
    assert click_comp["purl"] == f"pkg:pypi/click@{version}"


def test_metadata_component_is_target_dist() -> None:
    doc = build_sbom(LOCK, _policy(), "ne-cli")
    ne_cli_version = _lock_pkg("ne-cli")["version"]
    comp = doc["metadata"]["component"]
    assert comp["type"] == "application"
    assert comp["name"] == "ne-cli"
    assert comp["version"] == ne_cli_version
    assert comp["licenses"] == [{"expression": "Apache-2.0"}]
    tool = doc["metadata"]["tools"]["components"][0]
    assert tool["name"] == "ne-cli"
    assert tool["version"] == ne_cli_version


def test_components_are_exactly_the_member_runtime_closure() -> None:
    """A2 regression: per-member SBOMs are scoped to the member's actual
    transitive runtime dependency closure, not the whole workspace's
    third-party set. ne-cli's runtime closure is click -> colorama (colorama is
    win32-marker-gated but still IN the closure — the shipped artifact can
    install it) plus sqlalchemy -> greenlet/typing-extensions, PLUS (NE-069
    continuation, Codex P2-B) ne-contracts/ne-runtime/ne-govern's own
    transitive third-party closures — those workspace packages moved from a
    dev-only group into ne-cli's `[project].dependencies` so `ne release
    fingerprint`/`ne release bundle` work on a normal, non-dev install; their
    third-party deps (dbos, pydantic-ai-slim, opentelemetry, httpx, ...) are
    therefore now honestly part of ne-cli's shipped SBOM. NE-INGEST (P1-4)
    additionally adds ne-api + ne-connect to ne-cli's `[project].dependencies` (the
    advertised `ne connect sync` composes the governed platform + invokes the
    connectors), so their transitive closures (qdrant/onnxruntime/dlt/... — the
    heavy closure the brief explicitly accepts) are now honestly in the SBOM too.
    NE-FIX-SYNC-INVOKERS (P2-21) adds the OTLP/HTTP log-exporter family to ne-obs
    (googleapis-common-protos + opentelemetry-{proto,exporter-otlp-proto-common,
    exporter-otlp-proto-http}); ne-obs is already in ne-cli's closure via ne-api, so
    those Apache-2.0 packages are now honestly in the SBOM too. litellm 1.100.4
    (via ne-gateway) requires boto3, so boto3/botocore/jmespath/s3transfer
    (Apache-2.0 / MIT) are in the closure as well."""
    doc = build_sbom(LOCK, _policy(), "ne-cli")
    got = {c["name"] for c in doc["components"]}
    assert got == {
        "aiohappyeyeballs",
        "aiohttp",
        "aiosignal",
        "annotated-doc",
        "annotated-types",
        "anyio",
        "attrs",
        "boto3",
        "botocore",
        "certifi",
        "cffi",
        "charset-normalizer",
        "click",
        "colorama",
        "cronsim",
        "cryptography",
        "dbos",
        "distro",
        "dlt",
        "fastapi",
        "fastembed",
        "fastuuid",
        "filelock",
        "flatbuffers",
        "frozenlist",
        "fsspec",
        "genai-prices",
        "gitdb",
        "gitpython",
        "giturlparse",
        "googleapis-common-protos",
        "greenlet",
        "griffelib",
        "grpcio",
        "h11",
        "h2",
        "hf-xet",
        "hpack",
        "html5rdf",
        "httpcore",
        "httpcore2",
        "httpx",
        "httpx2",
        "httpx2-jsfetch",
        "huggingface-hub",
        "humanize",
        "hyperframe",
        "idna",
        "importlib-metadata",
        "jinja2",
        "jiter",
        "jmespath",
        "jsonpath-ng",
        "jsonschema",
        "jsonschema-specifications",
        "litellm",
        "logfire-api",
        "loguru",
        "markdown-it-py",
        "markupsafe",
        "mdurl",
        "mmh3",
        "multidict",
        "numpy",
        "onnxruntime",
        "openai",
        "openfga-sdk",
        "opentelemetry-api",
        "opentelemetry-exporter-otlp-proto-common",
        "opentelemetry-exporter-otlp-proto-http",
        "opentelemetry-proto",
        "opentelemetry-sdk",
        "opentelemetry-semantic-conventions",
        "orjson",
        "owlrl",
        "packaging",
        "pathvalidate",
        "pendulum",
        "pillow",
        "pluggy",
        "ply",
        "portalocker",
        "prettytable",
        "propcache",
        "protobuf",
        "psycopg",
        "psycopg-binary",
        "py-rust-stemmers",
        "pycparser",
        "pydantic",
        "pydantic-ai-slim",
        "pydantic-core",
        "pydantic-graph",
        "pydantic-settings",
        "pygments",
        "pyjwt",
        "pyodbc",
        "pyoxigraph",
        "pyparsing",
        "pyshacl",
        "python-dateutil",
        "python-dotenv",
        "pytz",
        "pywin32",
        "pyyaml",
        "qdrant-client",
        "rdflib",
        "referencing",
        "regex",
        "requests",
        "requirements-parser",
        "rich",
        "rich-argparse",
        "rpds-py",
        "s3transfer",
        "semver",
        "setuptools",
        "shellingham",
        "simplejson",
        "six",
        "smmap",
        "sniffio",
        "sqlalchemy",
        "sqlglot",
        "starlette",
        "tenacity",
        "tiktoken",
        "tokenizers",
        "tomlkit",
        "tqdm",
        "truststore",
        "typer",
        "typer-slim",
        "typing-extensions",
        "typing-inspection",
        "tzdata",
        "urllib3",
        "uvicorn",
        "wcwidth",
        "websockets",
        "win-precise-time",
        "win32-setctime",
        "yarl",
        "zipp",
    }
    # pytest is present in the workspace lock's third-party set but is NOT in
    # ne-cli's runtime closure (it's a workspace dev dependency everywhere).
    # pyyaml IS now genuinely in ne-cli's runtime closure — it arrives
    # transitively via ne-runtime -> dbos -> pyyaml, which is independent of
    # (and coincidental with) ne-cli's own pyyaml dev dependency.
    lock_third_party = {p["name"] for p in _lock()["package"] if not _is_first_party(p)}
    assert "pytest" in lock_third_party
    assert "pyyaml" in lock_third_party
    assert "pytest" not in got
    assert "pyyaml" in got
    assert not any(name.startswith("ne-") or name == "neutron-enterprise" for name in got)


def test_deterministic_output_modulo_serial_number() -> None:
    doc_a = build_sbom(LOCK, _policy(), "ne-cli")
    doc_b = build_sbom(LOCK, _policy(), "ne-cli")
    serial_a = doc_a.pop("serialNumber")
    serial_b = doc_b.pop("serialNumber")
    # serialNumber is a fresh urn:uuid v4 per build and is the ONLY difference.
    assert _UUID4_URN.match(serial_a)
    assert _UUID4_URN.match(serial_b)
    assert serial_a != serial_b
    assert json.dumps(doc_a) == json.dumps(doc_b)
    assert "timestamp" not in doc_a["metadata"]


def test_non_spdx_license_uses_name_form(monkeypatch) -> None:
    # A resolver returning a free-text license name -> {"license": {"name": ...}}.
    monkeypatch.setattr("ne.cli.sbom.resolve_license", lambda name, policy: "Some Custom License")
    doc = build_sbom(LOCK, _policy(), "ne-cli")
    comp = next(c for c in doc["components"] if "licenses" in c)
    assert comp["licenses"] == [{"license": {"name": "Some Custom License"}}]


def test_spdx_expression_kept() -> None:
    doc = build_sbom(LOCK, _policy(), "ne-cli")
    click_comp = next(c for c in doc["components"] if c["name"] == "click")
    assert click_comp["licenses"] == [{"expression": "BSD-3-Clause"}]


def test_is_spdx_expression_accepts() -> None:
    for expr in [
        "MIT",
        "MIT OR Apache-2.0",
        "Apache-2.0",
        "GPL-2.0-only WITH Classpath-exception-2.0",
        "(MIT OR Apache-2.0)",
        "Apache-2.0 AND (MIT OR BSD-3-Clause)",
    ]:
        assert _is_spdx_expression(expr), expr


def test_is_spdx_expression_rejects() -> None:
    for txt in [
        "BSD License",
        "MIT License",
        "Some Custom License",
        "",
        "   ",
        "MIT OR",
        "(MIT",
        "MIT)",
        "MIT and Apache-2.0",  # lowercase operator
    ]:
        assert not _is_spdx_expression(txt), txt


def test_unknown_package_raises(tmp_path: Path) -> None:
    try:
        build_sbom(LOCK, _policy(), "no-such-dist")
    except ValueError as exc:
        assert "no-such-dist" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError for unknown target")

    cwd = Path.cwd()
    try:
        os.chdir(REPO_ROOT)
        result = CliRunner().invoke(
            main,
            ["release", "sbom", "--package", "no-such-dist", "--out", str(tmp_path / "sbom")],
        )
    finally:
        os.chdir(cwd)
    assert result.exit_code != 0
    assert "no-such-dist" in result.output
    assert "ne-cli" in result.output  # member list is echoed


def test_cli_all_writes_one_sbom_per_member(tmp_path: Path) -> None:
    out = tmp_path / "sbom"
    cwd = Path.cwd()
    try:
        os.chdir(REPO_ROOT)
        result = CliRunner().invoke(main, ["release", "sbom", "--all", "--out", str(out)])
    finally:
        os.chdir(cwd)
    assert result.exit_code == 0, result.output
    members = [p for p in _lock()["package"] if _is_member(p)]
    files = sorted(out.glob("*.cdx.json"))
    assert len(files) == len(members)
    for f in files:
        doc = json.loads(f.read_text(encoding="utf-8"))
        assert doc["bomFormat"] == "CycloneDX"
        name = doc["metadata"]["component"]["name"]
        version = doc["metadata"]["component"]["version"]
        assert f.name == f"{name}-{version}.cdx.json"


def test_cli_requires_exactly_one_mode(tmp_path: Path) -> None:
    cwd = Path.cwd()
    try:
        os.chdir(REPO_ROOT)
        neither = CliRunner().invoke(main, ["release", "sbom", "--out", str(tmp_path / "a")])
        both = CliRunner().invoke(
            main,
            ["release", "sbom", "--package", "ne-cli", "--all", "--out", str(tmp_path / "b")],
        )
    finally:
        os.chdir(cwd)
    assert neither.exit_code != 0
    assert both.exit_code != 0


_SYNTH_HASH = "sha256:" + "c" * 64


def _write_synthetic_lock(tmp_path: Path, entries: list[dict]) -> Path:
    """Write a minimal synthetic uv.lock for closure-walk tests.

    Each entry dict may set: name (required), version, first_party (bool),
    dependencies (list of {"name": ..., "extra": [...]} dicts),
    optional_dependencies (dict extra-name -> list of dep dicts).
    """
    lines = ["version = 1", 'requires-python = ">=3.12"', ""]
    for e in entries:
        lines.append("[[package]]")
        lines.append(f'name = "{e["name"]}"')
        lines.append(f'version = "{e.get("version", "1.0.0")}"')
        if e.get("first_party"):
            lines.append(f'source = {{ editable = "tools/{e["name"]}" }}')
        else:
            lines.append('source = { registry = "https://pypi.org/simple" }')
            lines.append(
                f'sdist = {{ url = "https://example/{e["name"]}.tar.gz", hash = "{_SYNTH_HASH}" }}'
            )
        deps = e.get("dependencies")
        if deps:
            lines.append("dependencies = [")
            for dep in deps:
                extra = dep.get("extra")
                if extra:
                    extra_toml = ", ".join(f'"{x}"' for x in extra)
                    lines.append(f'  {{ name = "{dep["name"]}", extra = [{extra_toml}] }},')
                else:
                    lines.append(f'  {{ name = "{dep["name"]}" }},')
            lines.append("]")
        opt_deps = e.get("optional_dependencies")
        if opt_deps:
            lines.append("[package.optional-dependencies]")
            for extra_name, dep_list in opt_deps.items():
                dep_toml = ", ".join(f'{{ name = "{d["name"]}" }}' for d in dep_list)
                lines.append(f"{extra_name} = [{dep_toml}]")
        lines.append("")
    path = tmp_path / "uv.lock"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_synthetic_closure_excludes_unrelated_package(tmp_path: Path) -> None:
    """A diamond closure member -> a -> b plus an unrelated third-party c must
    resolve to exactly {a, b}; c must not leak in."""
    lock = _write_synthetic_lock(
        tmp_path,
        [
            {
                "name": "member",
                "first_party": True,
                "dependencies": [{"name": "a"}],
            },
            {"name": "a", "dependencies": [{"name": "b"}]},
            {"name": "b"},
            {"name": "c"},
        ],
    )
    doc = build_sbom(lock, _policy(), "member")
    got = {c["name"] for c in doc["components"]}
    assert got == {"a", "b"}


def test_synthetic_closure_follows_extras_edges(tmp_path: Path) -> None:
    """A dependency edge with extra=["x"] pulls d's optional-dependencies.x (e)
    but NOT d's optional-dependencies.y (f) — extras are per-edge, not global."""
    lock = _write_synthetic_lock(
        tmp_path,
        [
            {
                "name": "member",
                "first_party": True,
                "dependencies": [{"name": "d", "extra": ["x"]}],
            },
            {
                "name": "d",
                "optional_dependencies": {
                    "x": [{"name": "e"}],
                    "y": [{"name": "f"}],
                },
            },
            {"name": "e"},
            {"name": "f"},
        ],
    )
    doc = build_sbom(lock, _policy(), "member")
    got = {c["name"] for c in doc["components"]}
    assert got == {"d", "e"}
    assert "f" not in got


def test_workflows_wire_sbom() -> None:
    supply = (REPO_ROOT / ".github" / "workflows" / "supply-chain.yml").read_text(encoding="utf-8")
    release = (REPO_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "ne release sbom" in supply
    assert "ne release sbom" in release
    assert "OPEN DECISION" not in supply
    assert "OPEN DECISION" not in release
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in supply
    # NE-069R: release.yml must build the standalone SBOMs with deterministic
    # serials AND fail-closed verify their digests against the bundle manifest
    # before anything is signed — the guardian that release.yml can never
    # silently drift back to random-serial uploads or drop the verify step.
    assert "ne release sbom --all --deterministic --out dist/sbom" in release
    assert "Verify SBOM digests match the bundle manifest" in release
