# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for `ne k8s render` / `ne k8s up` (NE-071a K8s installer) with
kubectl/helm mocked (CI has no cluster). Bundle verification + secret
resolution run FOR REAL against a real `ne pilot promote` bundle + a
FileSecretStore fixture (plain codec) — mirrors test_up_cli.py's conventions.
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import types
from pathlib import Path

import pytest
from click.testing import CliRunner

from ne.cli import main
from ne.secrets import FileSecretStore

k8s_module = sys.modules["ne.cli.k8s"]
REPO_ROOT = Path(__file__).resolve().parents[3]
CHART_DIR = REPO_ROOT / "deploy" / "helm" / "neutron-enterprise"

_PG_SENTINEL = "PGsentinel-abc123"
_KC_SENTINEL = "KCsentinel-def456"
_OF_SENTINEL = "OFsentinel-ghi789"
_KEK_SENTINEL = "QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE"
_CONTINUATION_SENTINEL = "Q09OVElOVUFUSU9OQ09OVElOVUFUSU9OQ09OVElOVUE"
# The two key_ids are SECRET VALUES for the "never on argv" assertion below,
# which is a SUBSTRING test. They must therefore be long and distinctive like
# every other sentinel here. They used to be "k1"/"sa-k1", and a two-character
# needle matches by accident: any argv carrying a path or digest containing
# "k1" failed the assertion. That is why this test passed locally and FAILED on
# CI, where the runner's paths happen to contain it -- it blocked every merge
# from 2026-09-02 until it was found. Proven both ways: identical test, clean
# --basetemp passes, a --basetemp whose path contains "k1" fails.
_KEY_ID_SENTINEL = "KEYIDsentinel-jkl012"
_CONTINUATION_KEY_ID_SENTINEL = "SAKEYIDsentinel-mno345"
_PLATFORM_URL = "postgresql+psycopg://ne:secret@db.external.test:5432/neutron"
_DBOS_URL = "postgresql+psycopg://ne:secret@db.external.test:5432/ne_dbos"


def test_kind_smoke_provisions_external_protected_transport_bootstrap() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "helm-smoke.yml").read_text(encoding="utf-8")

    assert "postgres:18.6-alpine@sha256:" in workflow
    assert 'CI_EXTERNAL_POSTGRES_HOST="$(hostname -I' in workflow
    assert 'for database in ("neutron", "ne_dbos")' in workflow
    assert "--protected-platform-database-url" in workflow
    assert "--protected-dbos-database-url" in workflow
    assert "--admit-protected-transport" in workflow
    assert "@${CI_EXTERNAL_POSTGRES_HOST}:5432/neutron" in workflow
    assert "@${CI_EXTERNAL_POSTGRES_HOST}:5432/ne_dbos" in workflow


class _Recorder:
    """Fakes subprocess.run for kubectl/helm; records every call + stdin."""

    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[list[str]] = []
        self.inputs: list[str | None] = []

    def __call__(
        self, cmd: list[str], *, input: str | None = None, **_kwargs: object
    ) -> types.SimpleNamespace:
        self.calls.append(list(cmd))
        self.inputs.append(input)
        if cmd[:3] == ["kubectl", "create", "namespace"]:
            ns = cmd[3]
            stdout = json.dumps({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns}})
            return types.SimpleNamespace(returncode=self.returncode, stdout=stdout, stderr="")
        if cmd[:2] == ["helm", "list"]:
            return types.SimpleNamespace(returncode=self.returncode, stdout="[]", stderr="")
        return types.SimpleNamespace(returncode=self.returncode, stdout="", stderr="")


def _make_bundle(tmp_path: Path, *, seed: bool = True) -> Path:
    secrets_json = tmp_path / "secrets.ne.json"
    store = FileSecretStore(secrets_json)
    if seed:

        async def _seed() -> None:
            await store.write("pilot/postgres", {"password": _PG_SENTINEL})
            await store.write("pilot/keycloak", {"admin_password": _KC_SENTINEL})
            await store.write("pilot/openfga", {"api_token": _OF_SENTINEL})
            # PRE-009A (A2): the private-payload KEK is a REQUIRED secret, so a
            # bundle whose store lacks it now fails `ne up` by design.
            await store.write(
                "pilot/agent-run-private-payload-kek",
                {"key_id": _KEY_ID_SENTINEL, "kek": _KEK_SENTINEL},
            )
            await store.write(
                "pilot/source-authority-continuation",
                {"key_id": _CONTINUATION_KEY_ID_SENTINEL, "hmac_key_b64": _CONTINUATION_SENTINEL},
            )
            await store.write(
                "k8s/protected-transport-bootstrap",
                {
                    "platform_database_url": _PLATFORM_URL,
                    "dbos_database_url": _DBOS_URL,
                },
            )

        asyncio.run(_seed())
    else:
        asyncio.run(store.list_secrets(""))
    sec_toml = tmp_path / "ne-secrets.toml"
    sec_toml.write_text(f'[file]\npath = "{secrets_json}"\nmode = "plain"\n', encoding="utf-8")
    out = tmp_path / "bundle"
    result = CliRunner().invoke(
        main,
        [
            "pilot",
            "promote",
            "--customer",
            "t",
            "--hostname",
            "k8s.example.test",
            "--out",
            str(out),
            "--secrets-config",
            str(sec_toml),
        ],
    )
    assert result.exit_code == 0, result.output
    return out


# ---------------------------------------------------------------------------
# `ne k8s render`
# ---------------------------------------------------------------------------
def test_render_overlay_matches_manifest(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    out = tmp_path / "overlay.json"

    result = CliRunner().invoke(main, ["k8s", "render", "--bundle", str(bundle), "--out", str(out)])
    assert result.exit_code == 0, result.output

    overlay = json.loads(out.read_text(encoding="utf-8"))
    assert overlay["global"]["configHash"] == manifest["config_hash"]
    assert overlay["global"]["hostname"] == manifest["hostname"] == "k8s.example.test"
    for name in ("ne-runtime.toml", "ne-govern.toml", "ne-secrets.toml"):
        assert overlay["bundle"]["files"][name] == (bundle / "config" / name).read_text(
            encoding="utf-8"
        )

    # NE-PILOT-OPA-BUNDLE: the real ne-govern rego bundle rides the overlay too,
    # so the opa subchart's govern-policies ConfigMap + subPath mounts render.
    # `ne__config__data.json` is the DEPLOYMENT's OPA data document, carrying
    # `data.ne.config.approval_windows`. K8s must serve it for the same reason
    # Compose mounts it: without it `tiers.rego`'s 4 h built-in tier-2 floor
    # applies, and every tier-2 park on a 900 s session writes a four-hour
    # promise it strands as `authorization_expired` fifteen minutes later. (The
    # park-time coherence check does NOT catch that — it warns and parks on a
    # floor and refuses only DECLARED windows, which is exactly why the two tiers
    # must serve the same document. docs/architecture/approval-horizon.md.)
    govern_files = overlay["opa"]["governBundle"]["files"]
    assert set(govern_files) == {
        ".manifest",
        "ne__govern__decision.rego",
        "ne__govern__subagent.rego",
        "ne__govern__tiers.rego",
        "ne__govern__version.rego",
        "ne__config__data.json",
    }
    govern_source = REPO_ROOT / "packages" / "ne-govern" / "policies"
    data_source = REPO_ROOT / "deploy" / "opa" / "data"
    for key, spec in govern_files.items():
        root_dir = data_source if key.startswith("ne__config__") else govern_source
        source_path = root_dir / spec["path"]
        assert source_path.is_file(), spec["path"]
        assert spec["content"] == source_path.read_text(encoding="utf-8"), key
    assert "approval_windows" in json.loads(govern_files["ne__config__data.json"]["content"])


def test_render_rejects_tampered_bundle(tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    govern = bundle / "config" / "ne-govern.toml"
    govern.write_text(govern.read_text(encoding="utf-8") + "# tampered\n", encoding="utf-8")

    out = tmp_path / "overlay.json"
    result = CliRunner().invoke(main, ["k8s", "render", "--bundle", str(bundle), "--out", str(out)])
    assert result.exit_code != 0
    assert not out.exists()


# ---------------------------------------------------------------------------
# `ne k8s up`
# ---------------------------------------------------------------------------
def test_up_applies_secret_via_stdin_then_helm(monkeypatch, tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    rec = _Recorder()
    monkeypatch.setattr(k8s_module.subprocess, "run", rec)
    admissions: list[object] = []
    monkeypatch.setattr(
        k8s_module,
        "_prove_empty_external_databases",
        lambda admission: admissions.append(admission),
    )
    extra1 = tmp_path / "extra1.yaml"
    extra1.write_text("postgres:\n  storage:\n    size: 2Gi\n", encoding="utf-8")
    extra2 = tmp_path / "extra2.yaml"
    extra2.write_text("global:\n  imageRegistry: acr.example\n", encoding="utf-8")

    result = CliRunner().invoke(
        main,
        [
            "k8s",
            "up",
            "--bundle",
            str(bundle),
            "--chart",
            str(CHART_DIR),
            "--namespace",
            "neutron",
            "--release",
            "ne",
            "--admit-protected-transport",
            "-f",
            str(extra1),
            "-f",
            str(extra2),
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(admissions) == 1

    # namespace ensured (create --dry-run -> apply).
    ns_calls = [c for c in rec.calls if c[:2] == ["kubectl", "create"]]
    assert ns_calls == [
        ["kubectl", "create", "namespace", "neutron", "--dry-run=client", "-o", "json"]
    ]

    # Secret applied via kubectl apply -f - with resolved values on STDIN.
    apply_pairs = zip(rec.calls, rec.inputs, strict=True)
    apply_calls_with_input = [
        (cmd, inp) for cmd, inp in apply_pairs if cmd[:2] == ["kubectl", "apply"]
    ]
    secret_inputs = [
        inp for _cmd, inp in apply_calls_with_input if inp and '"kind": "Secret"' in inp
    ]
    assert len(secret_inputs) == 1
    secret_doc = json.loads(secret_inputs[0])
    assert secret_doc["metadata"]["name"] == "ne-platform-secrets"
    assert secret_doc["metadata"]["namespace"] == "neutron"
    assert secret_doc["stringData"]["NE_PILOT_POSTGRES_PASSWORD"] == _PG_SENTINEL
    assert secret_doc["stringData"]["NE_PILOT_KEYCLOAK_ADMIN_PASSWORD"] == _KC_SENTINEL
    # NE-087 (O-4): the always-required OpenFGA preshared key is in the Secret too.
    assert secret_doc["stringData"]["NE_PILOT_OPENFGA_API_TOKEN"] == _OF_SENTINEL
    assert secret_doc["stringData"]["NE_AGENT_TRANSPORT_GENERATION"] == "agent-transport-v1"
    assert secret_doc["stringData"]["NE_RUNTIME_SESSION_DATABASE_URL"] == _PLATFORM_URL
    assert secret_doc["stringData"]["NE_API_DBOS_SYSTEM_DATABASE_URL"] == _DBOS_URL
    assert secret_doc["stringData"]["NE_WORKER_DBOS_DATABASE_URL"] == _DBOS_URL

    # Secret values NEVER appear in any argv list.
    for cmd in rec.calls:
        joined = " ".join(cmd)
        for key, value in secret_doc["stringData"].items():
            if key != "NE_AGENT_TRANSPORT_GENERATION":
                assert value not in joined, key

    # helm upgrade --install args exact, -f ordering: rendered overlay first,
    # then user-supplied values in the order given (last wins).
    helm_calls = [c for c in rec.calls if c[:2] == ["helm", "upgrade"]]
    assert len(helm_calls) == 1
    helm_cmd = helm_calls[0]
    assert helm_cmd[:5] == ["helm", "upgrade", "--install", "ne", str(CHART_DIR)]
    assert "-n" in helm_cmd and helm_cmd[helm_cmd.index("-n") + 1] == "neutron"
    f_values = [helm_cmd[i + 1] for i, tok in enumerate(helm_cmd) if tok == "-f"]
    assert len(f_values) == 3  # rendered overlay + 2 user values
    assert f_values[1] == str(extra1)
    assert f_values[2] == str(extra2)  # user values last, in order given
    locked_index = helm_cmd.index("--set-string")
    assert helm_cmd[locked_index : locked_index + 4] == [
        "--set-string",
        "global.agentTransport.generation=agent-transport-v1",
        "--set",
        "global.agentTransport.emptyBootstrap=true",
    ]
    assert "--wait" in helm_cmd
    assert "--timeout" in helm_cmd


def test_up_aborts_before_helm_when_secret_resolution_fails(monkeypatch, tmp_path: Path) -> None:
    # seed=False -> the SecretStore has no entries, so resolve_required_secrets
    # raises (missing pilot secrets) BEFORE any kubectl/helm call.
    bundle = _make_bundle(tmp_path, seed=False)
    rec = _Recorder()
    monkeypatch.setattr(k8s_module.subprocess, "run", rec)

    result = CliRunner().invoke(
        main, ["k8s", "up", "--bundle", str(bundle), "--chart", str(CHART_DIR)]
    )
    assert result.exit_code != 0
    assert rec.calls == [], "no kubectl/helm mutation may occur when secret resolution fails"


def test_up_rejects_tampered_bundle(monkeypatch, tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["hostname"] = "attacker.example"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    rec = _Recorder()
    monkeypatch.setattr(k8s_module.subprocess, "run", rec)

    result = CliRunner().invoke(
        main, ["k8s", "up", "--bundle", str(bundle), "--chart", str(CHART_DIR)]
    )
    assert result.exit_code != 0
    assert rec.calls == []


def test_up_refuses_existing_release_before_cluster_mutation(monkeypatch, tmp_path: Path) -> None:
    bundle = _make_bundle(tmp_path)
    rec = _Recorder()

    def existing(
        cmd: list[str], *, input: str | None = None, **kwargs: object
    ) -> types.SimpleNamespace:
        result = rec(cmd, input=input, **kwargs)
        if cmd[:2] == ["helm", "list"]:
            result.stdout = '[{"name":"ne","status":"deployed"}]'
        elif cmd[:3] == ["helm", "get", "values"]:
            result.stdout = (
                '{"global":{"agentTransport":{'
                '"generation":"agent-transport-v1","emptyBootstrap":false}}}'
            )
        return result

    monkeypatch.setattr(k8s_module.subprocess, "run", existing)
    result = CliRunner().invoke(
        main,
        [
            "k8s",
            "up",
            "--bundle",
            str(bundle),
            "--chart",
            str(CHART_DIR),
            "--admit-protected-transport",
        ],
    )
    assert result.exit_code != 0
    assert rec.calls == [
        [
            "helm",
            "list",
            "--all",
            "--namespace",
            "neutron",
            "--filter",
            "^ne$",
            "--output",
            "json",
        ],
        [
            "helm",
            "get",
            "values",
            "ne",
            "--namespace",
            "neutron",
            "--output",
            "json",
        ],
    ]


@pytest.mark.parametrize(
    "release_record",
    [
        {"name": "ne", "status": "deployed", "revision": "1"},
        {"name": "ne", "status": "failed", "revision": "2"},
    ],
)
def test_up_upgrades_admitted_release_without_empty_inventory_proof(
    monkeypatch, tmp_path: Path, release_record: dict[str, str]
) -> None:
    bundle = _make_bundle(tmp_path)
    rec = _Recorder()
    live = {
        "NE_AGENT_TRANSPORT_GENERATION": "agent-transport-v1",
        "NE_RUNTIME_SESSION_DATABASE_URL": _PLATFORM_URL,
        "NE_API_DBOS_SYSTEM_DATABASE_URL": _DBOS_URL,
        "NE_WORKER_DBOS_DATABASE_URL": _DBOS_URL,
    }

    def admitted(
        cmd: list[str], *, input: str | None = None, **kwargs: object
    ) -> types.SimpleNamespace:
        result = rec(cmd, input=input, **kwargs)
        if cmd[:2] == ["helm", "list"]:
            result.stdout = json.dumps([release_record])
        elif cmd[:3] == ["helm", "get", "values"]:
            result.stdout = json.dumps(
                {
                    "global": {
                        "agentTransport": {
                            "generation": "agent-transport-v1",
                            "emptyBootstrap": True,
                        }
                    }
                }
            )
        elif cmd[:3] == ["kubectl", "get", "secret"]:
            result.stdout = json.dumps(
                {
                    "data": {
                        key: base64.b64encode(value.encode()).decode()
                        for key, value in live.items()
                    }
                }
            )
        return result

    monkeypatch.setattr(k8s_module.subprocess, "run", admitted)
    monkeypatch.setattr(
        k8s_module,
        "_prove_empty_external_databases",
        lambda _admission: pytest.fail("healthy upgrade must not require empty inventory"),
    )
    result = CliRunner().invoke(
        main,
        [
            "k8s",
            "up",
            "--bundle",
            str(bundle),
            "--chart",
            str(CHART_DIR),
            "--admit-protected-transport",
        ],
    )
    assert result.exit_code == 0, result.output
    assert any(call[:2] == ["helm", "upgrade"] for call in rec.calls)


def test_up_retries_failed_initial_release_only_with_same_markers_and_empty_inventory(
    monkeypatch, tmp_path: Path
) -> None:
    bundle = _make_bundle(tmp_path)
    rec = _Recorder()
    markers = {
        "NE_AGENT_TRANSPORT_GENERATION": "agent-transport-v1",
        "NE_RUNTIME_SESSION_DATABASE_URL": _PLATFORM_URL,
        "NE_API_DBOS_SYSTEM_DATABASE_URL": _DBOS_URL,
        "NE_WORKER_DBOS_DATABASE_URL": _DBOS_URL,
    }

    def failed(
        cmd: list[str], *, input: str | None = None, **kwargs: object
    ) -> types.SimpleNamespace:
        result = rec(cmd, input=input, **kwargs)
        if cmd[:2] == ["helm", "list"]:
            result.stdout = '[{"name":"ne","status":"failed","revision":"1"}]'
        elif cmd[:3] == ["helm", "get", "values"]:
            result.stdout = json.dumps(
                {
                    "global": {
                        "agentTransport": {
                            "generation": "agent-transport-v1",
                            "emptyBootstrap": True,
                        }
                    }
                }
            )
        elif cmd[:3] == ["kubectl", "get", "secret"]:
            result.stdout = json.dumps(
                {
                    "data": {
                        key: base64.b64encode(value.encode()).decode()
                        for key, value in markers.items()
                    }
                }
            )
        return result

    proofs: list[object] = []
    monkeypatch.setattr(k8s_module.subprocess, "run", failed)
    monkeypatch.setattr(
        k8s_module, "_prove_empty_external_databases", lambda admission: proofs.append(admission)
    )
    result = CliRunner().invoke(
        main, ["k8s", "up", "--bundle", str(bundle), "--chart", str(CHART_DIR)]
    )
    assert result.exit_code == 0, result.output
    assert len(proofs) == 1


def test_up_refuses_nonempty_or_unavailable_inventory_before_mutation(
    monkeypatch, tmp_path: Path
) -> None:
    bundle = _make_bundle(tmp_path)
    rec = _Recorder()
    monkeypatch.setattr(k8s_module.subprocess, "run", rec)

    def refuse(_admission: object) -> None:
        raise k8s_module.click.ClickException("legacy inventory is not empty")

    monkeypatch.setattr(k8s_module, "_prove_empty_external_databases", refuse)
    result = CliRunner().invoke(
        main,
        [
            "k8s",
            "up",
            "--bundle",
            str(bundle),
            "--chart",
            str(CHART_DIR),
            "--admit-protected-transport",
        ],
    )
    assert result.exit_code != 0
    assert rec.calls and all(call[:2] == ["helm", "list"] for call in rec.calls)


def test_inventory_proof_reads_both_exact_databases(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        k8s_module,
        "_prove_empty_database",
        lambda url, *, database: calls.append((url, database)),
    )
    k8s_module._prove_empty_external_databases(
        k8s_module._ProtectedBootstrapAdmission(_PLATFORM_URL, _DBOS_URL)
    )
    assert calls == [(_PLATFORM_URL, "neutron"), (_DBOS_URL, "ne_dbos")]


def test_inventory_proof_uses_timeout_engine_probes_and_always_disposes(monkeypatch) -> None:
    class Result:
        def __init__(
            self, *, rows: list[tuple[str, ...]] | None = None, scalar: object = None
        ) -> None:
            self._rows = [] if rows is None else rows
            self._scalar = scalar

        def __iter__(self):  # type: ignore[no-untyped-def]
            return iter(self._rows)

        def scalar_one(self) -> object:
            return self._scalar

    class Connection:
        def __init__(self) -> None:
            self.statements: list[str] = []

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, statement, _params=None):  # type: ignore[no-untyped-def]
            sql = str(statement)
            self.statements.append(sql)
            if "information_schema.tables" in sql:
                return Result(rows=[("ne_agent_session_messages",)])
            if "to_regclass" in sql:
                return Result(scalar="ne_agent_transport_generation")
            return Result(scalar=False)

    class Engine:
        def __init__(self, *, fail: bool = False) -> None:
            self.connection = Connection()
            self.fail = fail
            self.disposed = False

        def connect(self):  # type: ignore[no-untyped-def]
            if self.fail:
                raise RuntimeError("database unavailable")
            return self.connection

        def dispose(self) -> None:
            self.disposed = True

    admitted = Engine()
    calls: list[str] = []
    monkeypatch.setattr(
        "ne.audit.create_engine_with_timeouts",
        lambda url: calls.append(url) or admitted,
    )
    k8s_module._prove_empty_database(_PLATFORM_URL, database="neutron")
    assert calls == [_PLATFORM_URL]
    assert admitted.disposed
    assert any("information_schema.tables" in sql for sql in admitted.connection.statements)
    assert any(
        '"public"."ne_agent_session_messages"' in sql for sql in admitted.connection.statements
    )
    assert any("to_regclass" in sql for sql in admitted.connection.statements)
    assert any("ne_agent_transport_generation" in sql for sql in admitted.connection.statements)

    unavailable = Engine(fail=True)
    monkeypatch.setattr("ne.audit.create_engine_with_timeouts", lambda _url: unavailable)
    with pytest.raises(k8s_module.click.ClickException, match="inventory unavailable"):
        k8s_module._prove_empty_database(_PLATFORM_URL, database="neutron")
    assert unavailable.disposed


@pytest.mark.parametrize(
    ("platform_url", "dbos_url"),
    [
        ("postgresql://ne:x@postgres:5432/neutron", _DBOS_URL),
        (_PLATFORM_URL, "postgresql://ne:x@db.external.test:5432/wrong"),
    ],
)
def test_up_refuses_chart_local_or_wrong_database_connection(
    monkeypatch, tmp_path: Path, platform_url: str, dbos_url: str
) -> None:
    bundle = _make_bundle(tmp_path)
    config = bundle / "config" / "ne-secrets.toml"
    path = Path(config.read_text(encoding="utf-8").split('path = "', 1)[1].split('"', 1)[0])
    store = FileSecretStore(path)
    asyncio.run(
        store.write(
            "k8s/protected-transport-bootstrap",
            {"platform_database_url": platform_url, "dbos_database_url": dbos_url},
        )
    )
    rec = _Recorder()
    monkeypatch.setattr(k8s_module.subprocess, "run", rec)
    monkeypatch.setattr(k8s_module, "_prove_empty_external_databases", lambda _admission: None)
    result = CliRunner().invoke(
        main,
        [
            "k8s",
            "up",
            "--bundle",
            str(bundle),
            "--chart",
            str(CHART_DIR),
            "--admit-protected-transport",
        ],
    )
    assert result.exit_code != 0
    assert rec.calls and all(call[:2] == ["helm", "list"] for call in rec.calls)


@pytest.mark.parametrize("wait_flag", ["--no-wait"])
def test_up_no_wait_omits_wait_flags(monkeypatch, tmp_path: Path, wait_flag: str) -> None:
    bundle = _make_bundle(tmp_path)
    rec = _Recorder()
    monkeypatch.setattr(k8s_module.subprocess, "run", rec)
    monkeypatch.setattr(k8s_module, "_prove_empty_external_databases", lambda _admission: None)

    result = CliRunner().invoke(
        main, ["k8s", "up", "--bundle", str(bundle), "--chart", str(CHART_DIR), wait_flag]
    )
    assert result.exit_code == 0, result.output
    helm_cmd = next(c for c in rec.calls if c[:2] == ["helm", "upgrade"])
    assert "--wait" not in helm_cmd
