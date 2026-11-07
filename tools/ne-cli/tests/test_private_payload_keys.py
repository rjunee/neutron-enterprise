# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for `ne private-payload-keys` (PRE-009A A2 key lifecycle).

Self-contained by convention: `tools/ne-cli/tests/` has no conftest, so the bundle
and store fixtures are local here exactly as `test_k8s_cli.py` keeps its own.
The SecretStore is a real `FileSecretStore` (plain codec) and the rotation core
runs against a real in-memory SQLite runtime schema -- no mocked store, no mocked
rewrap, because the whole point of the rotation is what the database does.
"""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path

import pytest
import sqlalchemy as sa
from click.testing import CliRunner

from ne.cli import main
from ne.cli.agent_private_keys import (
    ENV_KEK_B64,
    ENV_KEY_ID,
    KEK_FIELD,
    KEK_LEN,
    KEK_PATH,
    KEY_ID_FIELD,
    decode_kek,
    encode_kek,
    kek_fingerprint,
)
from ne.secrets import FileSecretStore

REPO_ROOT = Path(__file__).resolve().parents[3]


def _bundle(tmp_path: Path) -> tuple[Path, FileSecretStore]:
    """A minimal bundle whose ne-secrets.toml points at a real FileSecretStore."""
    secrets_json = tmp_path / "secrets.ne.json"
    store = FileSecretStore(secrets_json)
    asyncio.run(store.list_secrets(""))  # materialize the file
    bundle = tmp_path / "bundle"
    (bundle / "config").mkdir(parents=True)
    (bundle / "config" / "ne-secrets.toml").write_text(
        f'[file]\npath = "{secrets_json}"\nmode = "plain"\n', encoding="utf-8"
    )
    return bundle, store


def _run(*args: str):
    return CliRunner().invoke(main, list(args))


def _read(store: FileSecretStore) -> dict[str, str]:
    return dict(asyncio.run(store.read(KEK_PATH)).data)


# --- canonical encoding -------------------------------------------------------


def test_encode_kek_is_unpadded_base64url() -> None:
    raw = bytes(range(32))
    encoded = encode_kek(raw)
    assert "=" not in encoded
    assert "+" not in encoded and "/" not in encoded
    assert decode_kek(encoded) == raw


@pytest.mark.parametrize(
    ("spelling", "why"),
    [
        (base64.urlsafe_b64encode(b"k" * 32).decode(), "padded"),
        (base64.b64encode(b"\xfb" * 32).decode().rstrip("="), "standard alphabet, not urlsafe"),
        (encode_kek(b"k" * 32) + "A", "wrong decoded length"),
        (encode_kek(b"k" * 31 + b"k")[:-2], "truncated"),
        ("", "empty"),
        ("not base64!", "not base64 at all"),
    ],
)
def test_decode_kek_rejects_every_non_canonical_spelling(spelling: str, why: str) -> None:
    """A key writable two ways is a key whose id can disagree with its bytes."""
    with pytest.raises(ValueError):
        decode_kek(spelling)
    # Positive control: the canonical spelling of the same length decodes.
    assert len(decode_kek(encode_kek(b"k" * KEK_LEN))) == KEK_LEN


def test_kek_fingerprint_is_non_reversible_and_discriminating() -> None:
    a, b = b"a" * 32, b"b" * 32
    assert kek_fingerprint(a) != kek_fingerprint(b)
    assert kek_fingerprint(a) == kek_fingerprint(a)
    printed = kek_fingerprint(a)
    assert encode_kek(a) not in printed
    assert a.hex() not in printed
    assert len(printed) == 16


# --- provision ----------------------------------------------------------------


def test_provision_writes_the_key_and_prints_only_a_fingerprint(tmp_path: Path) -> None:
    bundle, store = _bundle(tmp_path)
    result = _run("private-payload-keys", "provision", "--bundle", str(bundle))
    assert result.exit_code == 0, result.output
    data = _read(store)
    assert data[KEY_ID_FIELD] == "k1"
    raw = decode_kek(data[KEK_FIELD])
    assert len(raw) == KEK_LEN
    # The MATERIAL never appears in the output; the fingerprint does.
    assert data[KEK_FIELD] not in result.output
    assert raw.hex() not in result.output
    assert kek_fingerprint(raw) in result.output
    # The env-var NAMES are advertised so an operator can wire them.
    assert ENV_KEY_ID in result.output
    assert ENV_KEK_B64 in result.output


def test_provision_refuses_to_clobber_an_existing_key(tmp_path: Path) -> None:
    bundle, store = _bundle(tmp_path)
    assert _run("private-payload-keys", "provision", "--bundle", str(bundle)).exit_code == 0
    before = _read(store)
    result = _run("private-payload-keys", "provision", "--bundle", str(bundle))
    assert result.exit_code != 0
    assert "already exists" in result.output
    assert _read(store) == before  # untouched, not silently replaced


def test_provision_generates_a_distinct_key_each_time(tmp_path: Path) -> None:
    """CSPRNG, not a constant: two bundles must not share a key."""
    first, first_store = _bundle(tmp_path / "one")
    second, second_store = _bundle(tmp_path / "two")
    assert _run("private-payload-keys", "provision", "--bundle", str(first)).exit_code == 0
    assert _run("private-payload-keys", "provision", "--bundle", str(second)).exit_code == 0
    assert _read(first_store)[KEK_FIELD] != _read(second_store)[KEK_FIELD]


# --- status -------------------------------------------------------------------


def test_status_reports_identity_and_version_without_the_key(tmp_path: Path) -> None:
    bundle, store = _bundle(tmp_path)
    _run("private-payload-keys", "provision", "--bundle", str(bundle))
    data = _read(store)
    result = _run("private-payload-keys", "status", "--bundle", str(bundle))
    assert result.exit_code == 0, result.output
    assert "key_id=k1" in result.output
    assert "version=1" in result.output
    assert kek_fingerprint(decode_kek(data[KEK_FIELD])) in result.output
    assert data[KEK_FIELD] not in result.output


def test_status_on_an_unprovisioned_bundle_names_the_fix(tmp_path: Path) -> None:
    bundle, _store = _bundle(tmp_path)
    result = _run("private-payload-keys", "status", "--bundle", str(bundle))
    assert result.exit_code != 0
    assert KEK_PATH in result.output
    assert "provision" in result.output


# --- rotation core ------------------------------------------------------------


def _runtime_db(tmp_path: Path, key_id: str, kek: bytes, rows: int = 3) -> str:
    """A real runtime schema holding `rows` private payloads under `key_id`."""
    from ne.runtime.private_payloads import (
        AGENT_RUN_TURN_TARGET_KIND,
        CanonicalTargetTuple,
        PrivatePayloadDomain,
        ProtectedContentStatus,
        RunPrivatePayloadCipher,
        StaticPrivatePayloadKeys,
    )
    from ne.runtime.run_store import AgentRunStore, ensure_run_schema
    from ne.runtime.store import SqlAlchemySessionStore, ensure_runtime_schema

    url = f"sqlite:///{tmp_path / 'runtime.db'}"
    engine = sa.create_engine(url)
    ensure_runtime_schema(engine)
    ensure_run_schema(engine)
    cipher = RunPrivatePayloadCipher(
        StaticPrivatePayloadKeys(current_key_id=key_id, keys={key_id: kek})
    )
    sessions = SqlAlchemySessionStore(engine)
    runs = AgentRunStore(engine, cipher)

    async def _seed() -> None:
        conversation = await sessions.create_conversation("assistant", "claims", "alice")
        run = await runs.create_run(
            conversation_id=conversation.conversation_id,
            owner_principal_id="alice",
            department="claims",
            conversation_agent_name="assistant",
            agent_id="agt_01",
            agent_version=1,
            strategy="agentic",
            config_hash="1" * 64,
            capability_snapshot_hash="2" * 64,
            capability_snapshot_receipt_id="cap-1",
            input_plaintext=b"private prompt",
        )
        attempt = await runs.link_attempt(run_id=run.run_id)
        for index in range(rows - 1):
            await runs.claim_protected_payload(
                domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
                run_id=run.run_id,
                owner_principal_id="alice",
                target_kind=AGENT_RUN_TURN_TARGET_KIND,
                target=CanonicalTargetTuple(
                    conversation_id=conversation.conversation_id,
                    run_id=run.run_id,
                    turn_id=f"turn-{index}",
                    turn_sequence=index + 1,
                    attachment_id=f"attach-{index}",
                    attempt_id=attempt.attempt_id,
                ),
                content_status=ProtectedContentStatus.SOURCE_FREE,
                plaintext=f"protected {index}".encode(),
            )

    asyncio.run(_seed())
    engine.dispose()
    return url


def _key_ids(url: str) -> dict[str, int]:
    from ne.runtime.run_store import ne_agent_protected_turn_contents, ne_agent_run_inputs

    engine = sa.create_engine(url)
    counts: dict[str, int] = {}
    try:
        with engine.connect() as conn:
            for table in (ne_agent_run_inputs, ne_agent_protected_turn_contents):
                for key_id, count in conn.execute(
                    sa.select(table.c.key_id, sa.func.count()).group_by(table.c.key_id)
                ):
                    counts[str(key_id)] = counts.get(str(key_id), 0) + int(count)
    finally:
        engine.dispose()
    return counts


def _provisioned(tmp_path: Path) -> tuple[Path, FileSecretStore, str, bytes]:
    bundle, store = _bundle(tmp_path)
    assert _run("private-payload-keys", "provision", "--bundle", str(bundle)).exit_code == 0
    data = _read(store)
    return bundle, store, data[KEY_ID_FIELD], decode_kek(data[KEK_FIELD])


def test_rotate_refuses_unless_both_roots_are_declared_stopped(tmp_path: Path) -> None:
    """A live root keeps minting rows under the old key while the rewrap walks."""
    bundle, store, key_id, kek = _provisioned(tmp_path)
    url = _runtime_db(tmp_path, key_id, kek)
    before = _read(store)
    result = _run("private-payload-keys", "rotate", "--bundle", str(bundle), "--database-url", url)
    assert result.exit_code != 0
    assert "--roots-stopped" in result.output
    assert _read(store) == before  # the secret is untouched
    assert _key_ids(url) == {key_id: 3}  # and so are the rows


def test_rotate_rewraps_every_row_and_leaves_zero_old_references(tmp_path: Path) -> None:
    bundle, store, key_id, kek = _provisioned(tmp_path)
    url = _runtime_db(tmp_path, key_id, kek)
    assert _key_ids(url) == {key_id: 3}
    result = _run(
        "private-payload-keys",
        "rotate",
        "--bundle",
        str(bundle),
        "--database-url",
        url,
        "--roots-stopped",
    )
    assert result.exit_code == 0, result.output
    after = _read(store)
    assert after[KEY_ID_FIELD] == "k2"
    new_kek = decode_kek(after[KEK_FIELD])
    assert new_kek != kek
    # EXACT SecretVersions, the rewrapped row count, and zero old references.
    assert "version=1->2" in result.output
    assert "k1->k2" in result.output
    assert "rewrapped=3" in result.output
    assert "old_references=0" in result.output
    assert _key_ids(url) == {"k2": 3}
    # Never the material; and the operator is told to refresh and restart.
    assert after[KEK_FIELD] not in result.output
    assert kek_fingerprint(new_kek) in result.output
    assert "restart" in result.output


def test_rotated_payloads_are_still_readable_under_the_new_key_only(tmp_path: Path) -> None:
    """The rewrap re-seals the DEK; the payload must survive it, and the OLD key
    must stop working -- otherwise the rotation is decorative."""
    from ne.runtime.private_payloads import RunPrivatePayloadCipher, StaticPrivatePayloadKeys
    from ne.runtime.run_store import AgentRunStore

    bundle, store, key_id, kek = _provisioned(tmp_path)
    url = _runtime_db(tmp_path, key_id, kek)
    engine = sa.create_engine(url)
    runs_before = AgentRunStore(
        engine, RunPrivatePayloadCipher(StaticPrivatePayloadKeys(key_id, {key_id: kek}))
    )
    run_id = asyncio.run(_first_run_id(engine))
    original = asyncio.run(
        runs_before.materialize_run_input(run_id=run_id, owner_principal_id="alice")
    )
    engine.dispose()

    assert (
        _run(
            "private-payload-keys",
            "rotate",
            "--bundle",
            str(bundle),
            "--database-url",
            url,
            "--roots-stopped",
        ).exit_code
        == 0
    )
    new = _read(store)
    new_id, new_kek = new[KEY_ID_FIELD], decode_kek(new[KEK_FIELD])

    engine = sa.create_engine(url)
    try:
        after = AgentRunStore(
            engine, RunPrivatePayloadCipher(StaticPrivatePayloadKeys(new_id, {new_id: new_kek}))
        )
        assert (
            asyncio.run(after.materialize_run_input(run_id=run_id, owner_principal_id="alice"))
            == original
        )
        stale = AgentRunStore(
            engine, RunPrivatePayloadCipher(StaticPrivatePayloadKeys(key_id, {key_id: kek}))
        )
        with pytest.raises(Exception):  # noqa: B017 - any refusal; it must not succeed
            asyncio.run(stale.materialize_run_input(run_id=run_id, owner_principal_id="alice"))
    finally:
        engine.dispose()


async def _first_run_id(engine: sa.Engine) -> str:
    from ne.runtime.run_store import ne_agent_runs

    with engine.connect() as conn:
        return str(conn.execute(sa.select(ne_agent_runs.c.run_id)).scalar_one())


def test_rotate_publishes_nothing_when_the_rewrap_refuses(tmp_path: Path) -> None:
    """Ordering carries the safety: the SecretStore has no CAS, so the new key is
    published only AFTER the database rewrap succeeds. A published key whose rows
    were never rewrapped is unrecoverable; a rewrap with an unpublished key is
    simply re-runnable."""
    bundle, store, key_id, kek = _provisioned(tmp_path)
    url = _runtime_db(tmp_path, key_id, kek)
    # Plant a row under an UNEXPECTED key id, which the database-side
    # compare-and-swap refuses to rewrap.
    from ne.runtime.run_store import ne_agent_run_inputs

    engine = sa.create_engine(url)
    with engine.begin() as conn:
        conn.execute(ne_agent_run_inputs.update().values(key_id="kX"))
    engine.dispose()
    before = _read(store)
    result = _run(
        "private-payload-keys",
        "rotate",
        "--bundle",
        str(bundle),
        "--database-url",
        url,
        "--roots-stopped",
    )
    assert result.exit_code != 0
    assert "rotation refused" in result.output
    assert _read(store) == before  # the key was NOT published


def test_rotate_on_an_unprovisioned_bundle_refuses(tmp_path: Path) -> None:
    bundle, _store = _bundle(tmp_path)
    url = f"sqlite:///{tmp_path / 'empty.db'}"
    result = _run(
        "private-payload-keys",
        "rotate",
        "--bundle",
        str(bundle),
        "--database-url",
        url,
        "--roots-stopped",
    )
    assert result.exit_code != 0
    assert "nothing to rotate" in result.output


# --- delivery wiring ----------------------------------------------------------


def test_the_key_is_a_required_secret_by_name_only(tmp_path: Path) -> None:
    """NAMES and paths in REQUIRED_SECRETS; the value lives only in the store."""
    from ne.cli.pilotcfg import REQUIRED_SECRETS

    assert REQUIRED_SECRETS[ENV_KEY_ID] == {"path": KEK_PATH, "field": KEY_ID_FIELD}
    assert REQUIRED_SECRETS[ENV_KEK_B64] == {"path": KEK_PATH, "field": KEK_FIELD}
    source = Path(REPO_ROOT / "tools" / "ne-cli" / "src" / "ne" / "cli" / "pilotcfg.py").read_text(
        encoding="utf-8"
    )
    assert "kek=" not in source


def test_up_scrubs_an_ambient_private_payload_key_from_the_child_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ambient host KEK must never beat the bundle-resolved value.

    Same failure the NE_PILOT_ENTRA_ prefix fixed, with worse consequences: a stale
    ambient key makes every stored private row unopenable.
    """
    from ne.cli.up import _scrubbed_child_env

    monkeypatch.setenv(ENV_KEY_ID, "ambient-key-id")
    monkeypatch.setenv(ENV_KEK_B64, "ambient-kek")
    monkeypatch.setenv("NE_PILOT_HOSTNAME", "kept.example.com")
    child = _scrubbed_child_env(REPO_ROOT)
    assert ENV_KEY_ID not in child
    assert ENV_KEK_B64 not in child
    # Positive control: an unrelated variable IS passed through, so the scrub is
    # targeted rather than a wholesale empty environment.
    assert child["NE_PILOT_HOSTNAME"] == "kept.example.com"


def test_the_cli_module_import_does_not_pull_in_the_runtime(tmp_path: Path) -> None:
    """House style: `ne --help` stays fast, so ne.runtime is imported lazily."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import sys\n"
        "import ne.cli.agent_private_keys  # noqa: F401\n"
        "print(json.dumps([m for m in sys.modules if m.startswith('ne.runtime')]))\n".replace(
            "json.dumps", "__import__('json').dumps"
        ),
        encoding="utf-8",
    )
    import subprocess
    import sys

    out = subprocess.run(  # fixed argv, no shell
        [sys.executable, str(probe)], capture_output=True, text=True, cwd=REPO_ROOT, check=True
    )
    assert json.loads(out.stdout) == []
