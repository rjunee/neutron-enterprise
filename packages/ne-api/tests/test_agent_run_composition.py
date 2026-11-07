# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""PRE-009A A3: the conditional-mandatory Agent Run composition boundary.

The discriminator between "dormant" and "activated" is itself security-relevant:
if it can be made to read *not activated* on a deployment that genuinely is, the
whole fail-closed property inverts into fail-open. These controls therefore attack
the predicate directly -- each activation signal is exercised ALONE, and the
retained-row signal is exercised with NO key configured at all, which is exactly
the path a "key absent means dormant" shortcut would walk straight past.

Composition is tested through `_compose_agent_run_store` rather than a full
`build_platform`, so this file adds no API/worker conftest scope.
"""

from __future__ import annotations

import base64

import pytest
import sqlalchemy as sa

from ne.api.platform import (
    AGENT_PRIVATE_PAYLOAD_KEK_ENV,
    AGENT_PRIVATE_PAYLOAD_KEY_ID_ENV,
    _compose_agent_run_store,
    _private_payload_tables,
    _retained_private_payload_key_ids,
)
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

_KEK = b"K" * 32
_OTHER_KEK = b"O" * 32


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _engine(tmp_path) -> sa.Engine:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'runtime.db'}")
    ensure_runtime_schema(engine)
    ensure_run_schema(engine)
    return engine


def _seed_row(engine: sa.Engine, *, key_id: str, kek: bytes) -> None:
    """One retained private row wrapped under `key_id`."""
    import asyncio

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
        await runs.claim_protected_payload(
            domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
            run_id=run.run_id,
            owner_principal_id="alice",
            target_kind=AGENT_RUN_TURN_TARGET_KIND,
            target=CanonicalTargetTuple(
                conversation_id=conversation.conversation_id,
                run_id=run.run_id,
                turn_id="turn-1",
                turn_sequence=1,
                attachment_id="attach-1",
                attempt_id=attempt.attempt_id,
            ),
            content_status=ProtectedContentStatus.SOURCE_FREE,
            plaintext=b"the assistant said this",
        )

    asyncio.run(_seed())


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(AGENT_PRIVATE_PAYLOAD_KEY_ID_ENV, raising=False)
    monkeypatch.delenv(AGENT_PRIVATE_PAYLOAD_KEK_ENV, raising=False)


def _set_key(monkeypatch: pytest.MonkeyPatch, key_id: str, kek_b64: str) -> None:
    monkeypatch.setenv(AGENT_PRIVATE_PAYLOAD_KEY_ID_ENV, key_id)
    monkeypatch.setenv(AGENT_PRIVATE_PAYLOAD_KEK_ENV, kek_b64)


# --- the envelope-table inventory is DERIVED, not enumerated -------------------


def test_the_private_payload_table_inventory_is_derived_from_the_schema(tmp_path) -> None:
    """A hand-written list of domains silently stops seeing an eighth one."""
    from ne.runtime.run_store import _PROTECTED_TABLES  # the store's own source of truth

    derived = {table.name for table in _private_payload_tables()}
    expected = {table.name for table, _row_id, _kinds in _PROTECTED_TABLES.values()}
    expected |= {"ne_agent_run_inputs", "ne_agent_evidence_locators"}
    assert derived == expected
    assert len(derived) == 7  # the seven private domains PRE-009A owns


def test_the_inventory_reads_key_ids_without_constructing_a_cipher(tmp_path) -> None:
    engine = _engine(tmp_path)
    assert _retained_private_payload_key_ids(engine) == set()
    _seed_row(engine, key_id="k1", kek=_KEK)
    assert _retained_private_payload_key_ids(engine) == {"k1"}
    engine.dispose()


# --- 1. dormant ---------------------------------------------------------------


def test_wholly_unconfigured_and_empty_composes_dormant(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No signal at all: dormant, so unrelated build_platform callers are unchanged."""
    _clear_env(monkeypatch)
    engine = _engine(tmp_path)
    assert _compose_agent_run_store(engine, deployment_configured=False) is None
    engine.dispose()


# --- 2. each activation signal ALONE makes the key mandatory ------------------


def test_a_configured_deployment_with_no_key_refuses(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    engine = _engine(tmp_path)
    with pytest.raises(ValueError) as raised:
        _compose_agent_run_store(engine, deployment_configured=True)
    assert AGENT_PRIVATE_PAYLOAD_KEY_ID_ENV in str(raised.value)
    assert AGENT_PRIVATE_PAYLOAD_KEK_ENV in str(raised.value)
    engine.dispose()


@pytest.mark.parametrize(
    "present", [AGENT_PRIVATE_PAYLOAD_KEY_ID_ENV, AGENT_PRIVATE_PAYLOAD_KEK_ENV]
)
def test_a_half_configured_deployment_activates_and_then_refuses(
    tmp_path, monkeypatch: pytest.MonkeyPatch, present: str
) -> None:
    """EITHER variable alone activates. A half-configured deployment must not read
    as unconfigured -- that is the exact inversion that turns fail-closed into
    fail-open."""
    _clear_env(monkeypatch)
    monkeypatch.setenv(present, "k1" if present == AGENT_PRIVATE_PAYLOAD_KEY_ID_ENV else _b64(_KEK))
    engine = _engine(tmp_path)
    with pytest.raises(ValueError) as raised:
        _compose_agent_run_store(engine, deployment_configured=False)
    missing = (
        AGENT_PRIVATE_PAYLOAD_KEK_ENV
        if present == AGENT_PRIVATE_PAYLOAD_KEY_ID_ENV
        else AGENT_PRIVATE_PAYLOAD_KEY_ID_ENV
    )
    assert missing in str(raised.value)
    engine.dispose()


def test_retained_rows_with_no_key_configured_refuse(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE clause that closes the dormant-path hole: a database holding private
    rows must never be walked past just because no key is configured."""
    engine = _engine(tmp_path)
    _seed_row(engine, key_id="k1", kek=_KEK)
    _clear_env(monkeypatch)
    with pytest.raises(ValueError) as raised:
        _compose_agent_run_store(engine, deployment_configured=False)
    assert AGENT_PRIVATE_PAYLOAD_KEY_ID_ENV in str(raised.value)
    engine.dispose()


def test_retained_rows_under_another_key_block_readiness(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine(tmp_path)
    _seed_row(engine, key_id="kOLD", kek=_OTHER_KEK)
    _set_key(monkeypatch, "kNEW", _b64(_KEK))
    with pytest.raises(ValueError, match="wrapped under a key other than"):
        _compose_agent_run_store(engine, deployment_configured=False)
    engine.dispose()


# --- 3. malformed key material ------------------------------------------------


@pytest.mark.parametrize(
    ("spelling", "why"),
    [
        (base64.urlsafe_b64encode(_KEK).decode(), "padded"),
        (base64.b64encode(b"\xfb" * 32).decode().rstrip("="), "standard alphabet"),
        (_b64(b"K" * 16), "wrong length"),
        ("not base64!", "not base64"),
    ],
)
def test_a_noncanonical_or_wrong_length_key_refuses(
    tmp_path, monkeypatch: pytest.MonkeyPatch, spelling: str, why: str
) -> None:
    _set_key(monkeypatch, "k1", spelling)
    engine = _engine(tmp_path)
    with pytest.raises(ValueError) as raised:
        _compose_agent_run_store(engine, deployment_configured=False)
    # The message names the VARIABLE and never echoes the value.
    assert AGENT_PRIVATE_PAYLOAD_KEK_ENV in str(raised.value)
    assert spelling not in str(raised.value)
    engine.dispose()


# --- 4. the matching current key composes -------------------------------------


def test_the_matching_current_key_composes_the_store(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine(tmp_path)
    _seed_row(engine, key_id="k1", kek=_KEK)
    _set_key(monkeypatch, "k1", _b64(_KEK))
    store = _compose_agent_run_store(engine, deployment_configured=True)
    assert store is not None
    readiness = store.current_key_readiness()
    assert readiness.ready is True
    assert readiness.current_key_id == "k1"
    assert readiness.rows_under_other_keys == 0
    assert readiness.rows_under_current_key > 0
    engine.dispose()


def test_an_empty_database_with_a_matching_key_composes(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh deployment activates and composes with zero retained rows."""
    _set_key(monkeypatch, "k1", _b64(_KEK))
    engine = _engine(tmp_path)
    store = _compose_agent_run_store(engine, deployment_configured=False)
    assert store is not None
    assert store.current_key_readiness().ready is True
    engine.dispose()


def test_the_key_material_never_reaches_the_frozen_config_hash() -> None:
    """The key is parsed AFTER freeze_platform_config, by construction.

    Asserted structurally rather than by running two boots: `freeze_platform_config`
    must appear BEFORE the composition call in build_platform's source, and neither
    env name may appear in the freeze arguments.
    """
    import inspect

    from ne.api import platform as platform_module

    source = inspect.getsource(platform_module.build_platform)
    freeze_at = source.index("freeze_platform_config(")
    compose_at = source.index("_compose_agent_run_store(")
    assert freeze_at < compose_at
    frozen_args = source[freeze_at : source.index(")", freeze_at)]
    assert AGENT_PRIVATE_PAYLOAD_KEY_ID_ENV not in frozen_args
    assert AGENT_PRIVATE_PAYLOAD_KEK_ENV not in frozen_args
