# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""015A0-AUTHORITY-CORE: the HMAC continuation codec and its private durable rows.

The ADR-0022 §7 codec kit is subclassed with the real codec. Everything else here
holds the three properties the kit cannot see, because they are about the ROWS and
the ORDER rather than the token bytes:

* the persisted column sets are exactly the non-secret canonical state and the
  replay integrity record — asserted as set EQUALITY, so adding a key, token,
  payload or source-label column fails here rather than passing a presence check;
* a well-formed token is claim/loaded BEFORE its MAC is authenticated, and the MAC
  is re-derived from the row that was actually loaded, so an edited row fails
  instead of being served;
* a token whose key id is not this replica's fails before any lookup, while every
  other unusable token performs exactly one lookup and yields the same opaque
  outcome — a per-cause message or a per-cause lookup count is the oracle.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
import sqlalchemy as sa

from ne.brain.current_source_authority import (
    CONTINUATION_CODEC_VERSION,
    ContinuationUnavailableError,
    HmacAuthorizedContinuationTokenCodec,
    SqlAlchemyAuthorizedContinuationStore,
    ensure_source_authority_schema,
    ne_authorized_continuation_replays,
    ne_authorized_continuation_states,
)
from ne.contracts.source_authority import (
    AuthorizedArtifactPageStatus,
    AuthorizedContinuationReplay,
    AuthorizedContinuationState,
    AuthorizedContinuationTokenCodec,
)
from ne.contracts.testing.source_authority import AuthorizedContinuationTokenCodecContractKit

KEY = b"continuation-key-exactly-32-byte"
OTHER_KEY = b"another-key-also-exactly-32-byte"
KEY_ID = "pilot-k1"


def memory_engine() -> sa.Engine:
    engine = sa.create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=sa.pool.StaticPool,
    )
    ensure_source_authority_schema(engine)
    return engine


def continuation_state(
    *, expires_in_seconds: int = 3600, scan_limit: int = 50
) -> AuthorizedContinuationState:
    return AuthorizedContinuationState(
        state_version=1,
        principal_access_hash="1" * 64,
        request_hash="2" * 64,
        normalizer_version=1,
        encoding_version=1,
        requested_top_k=10,
        scan_limit=scan_limit,
        config_hash="3" * 64,
        contract_head_hash="4" * 64,
        cursor_score=Decimal("0.75"),
        cursor_artifact_id="5" * 64,
        prior_page_set_hash="6" * 64,
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in_seconds),
    )


def _refusal(store: SqlAlchemyAuthorizedContinuationStore, token: str) -> str:
    """The caller-visible outcome fingerprint of one unusable continuation."""
    with pytest.raises(ContinuationUnavailableError) as raised:
        store.claim_load(token)
    return f"{type(raised.value).__name__}|{raised.value}"


# --- ADR-0022 §7 codec conformance kit, subclassed with the real codec -------


class TestAuthorizedContinuationTokenCodecContract(AuthorizedContinuationTokenCodecContractKit):
    def create_codec(self) -> AuthorizedContinuationTokenCodec:
        return HmacAuthorizedContinuationTokenCodec(KEY_ID, KEY)

    def state(self) -> AuthorizedContinuationState:
        return continuation_state()

    def mutate_token(self, token: str) -> str:
        key_id, _, mac = token.partition(".")
        raw = base64.urlsafe_b64decode(mac + "=")
        flipped = bytes((raw[0] ^ 1, *raw[1:]))
        return f"{key_id}.{base64.urlsafe_b64encode(flipped).rstrip(b'=').decode()}"

    def decode_failure(self, codec: AuthorizedContinuationTokenCodec, token: str) -> bool:
        try:
            codec.decode(token)
        except ValueError:
            return True
        return False


# --- the private rows carry no key, token, payload or source label -----------


def test_the_continuation_state_row_holds_exactly_the_canonical_state() -> None:
    """Set EQUALITY, not presence: a new column has to be justified here."""
    assert set(ne_authorized_continuation_states.c.keys()) == {
        "key_id",
        "token_mac",
        "state_version",
        "principal_access_hash",
        "request_hash",
        "normalizer_version",
        "encoding_version",
        "requested_top_k",
        "scan_limit",
        "config_hash",
        "contract_head_hash",
        "cursor_score",
        "cursor_artifact_id",
        "prior_page_set_hash",
        "expires_at",
    }


def test_the_replay_row_holds_only_the_first_resolution_integrity_record() -> None:
    """No item ids, contributor stamps or ACL fingerprints reach the row.

    A replay is RE-DERIVED and checked against this hash; storing the disclosed
    material would make the private row a cache of protected content.
    """
    assert set(ne_authorized_continuation_replays.c.keys()) == {
        "token_mac",
        "replay_hash",
        "status",
        "scanned_count",
        "returned_count",
        "scan_limit",
        "scope_health_epoch",
        "next_state_hash",
        "created_at",
    }


def test_the_schema_holds_no_cluster_key_head_or_replica_convergence_row() -> None:
    """CORE stores no fictitious cluster-wide key state.

    Set EQUALITY over the whole metadata, so a `..._key_head` or
    `..._replica_convergence` table cannot be added without answering here. A
    per-row `key_id` is the issuing key of THAT continuation; a cluster head would
    be a claim about other replicas this deployment cannot make.
    """
    from ne.brain.current_source_authority import source_authority_metadata

    assert set(source_authority_metadata.tables) == {
        "ne_source_authority_deployment",
        "ne_source_authority_receipts",
        "ne_source_authority_heads",
        "ne_source_authority_acl_snapshots",
        "ne_source_authority_contract_epochs",
        "ne_source_reconciliation_claims",
        "ne_authorized_continuation_states",
        "ne_authorized_continuation_replays",
    }


def test_no_persisted_continuation_value_is_the_key_or_the_presented_token() -> None:
    engine = memory_engine()
    codec = HmacAuthorizedContinuationTokenCodec(KEY_ID, KEY)
    store = SqlAlchemyAuthorizedContinuationStore(engine, codec)
    token = store.persist(continuation_state())
    forbidden = {
        token,
        KEY.decode("latin-1"),
        base64.urlsafe_b64encode(KEY).rstrip(b"=").decode(),
        KEY.hex(),
    }
    with engine.connect() as conn:
        row = conn.execute(sa.select(ne_authorized_continuation_states)).one()
    cells = {str(value) for value in row}
    assert cells.isdisjoint(forbidden)
    # Positive control: the row IS populated, so the disjointness above is a real
    # exclusion rather than an empty scan.
    assert row.key_id == KEY_ID
    assert row.token_mac == codec.token_mac(
        continuation_state().model_copy(update={"expires_at": row.expires_at.replace(tzinfo=UTC)})
    )


# --- claim/load ordering, key-id fencing and one opaque outcome --------------


def test_a_valid_token_claim_loads_its_private_row_and_returns_the_state() -> None:
    """The must-PASS half: without it every refusal control below is vacuous."""
    engine = memory_engine()
    store = SqlAlchemyAuthorizedContinuationStore(
        engine, HmacAuthorizedContinuationTokenCodec(KEY_ID, KEY)
    )
    state = continuation_state()
    token = store.persist(state)
    loaded = store.claim_load(token)
    assert loaded.request_hash == state.request_hash
    assert loaded.scan_limit == state.scan_limit
    assert loaded.cursor_score == state.cursor_score
    assert store.row_lookup_count == 1


def test_a_well_formed_unknown_mac_is_still_claim_loaded_before_it_is_refused() -> None:
    """ADR-0022 §5 order: no pre-filter may answer ahead of the private row.

    An in-memory set of issued MACs, a bloom filter or a prefix check would all
    refuse this token with ZERO lookups — and all three are timing oracles over
    which continuations exist.
    """
    engine = memory_engine()
    codec = HmacAuthorizedContinuationTokenCodec(KEY_ID, KEY)
    store = SqlAlchemyAuthorizedContinuationStore(engine, codec)
    store.persist(continuation_state())
    unknown = f"{KEY_ID}.{base64.urlsafe_b64encode(bytes(range(32))).rstrip(b'=').decode()}"
    _refusal(store, unknown)
    assert store.row_lookup_count == 1


def test_a_foreign_key_id_is_refused_before_any_private_row_lookup() -> None:
    """It fails BEFORE lookup, not merely fails."""
    engine = memory_engine()
    codec = HmacAuthorizedContinuationTokenCodec(KEY_ID, KEY)
    store = SqlAlchemyAuthorizedContinuationStore(engine, codec)
    token = store.persist(continuation_state())
    _, _, mac = token.partition(".")
    _refusal(store, f"some-other-key-id.{mac}")
    assert store.row_lookup_count == 0


def test_a_tampered_private_row_fails_authentication_after_it_is_loaded() -> None:
    """The MAC is re-derived from the row that was loaded, not from the token.

    This is what makes the claim/load-then-authenticate order load-bearing rather
    than stylistic: an edited state column cannot be detected before the row
    exists in hand.
    """
    engine = memory_engine()
    codec = HmacAuthorizedContinuationTokenCodec(KEY_ID, KEY)
    store = SqlAlchemyAuthorizedContinuationStore(engine, codec)
    token = store.persist(continuation_state())
    with engine.begin() as conn:
        conn.execute(sa.update(ne_authorized_continuation_states).values(scan_limit=999))
    _refusal(store, token)
    assert store.row_lookup_count == 1


def test_an_expired_continuation_is_refused() -> None:
    engine = memory_engine()
    codec = HmacAuthorizedContinuationTokenCodec(KEY_ID, KEY)
    store = SqlAlchemyAuthorizedContinuationStore(engine, codec)
    token = store.persist(continuation_state(expires_in_seconds=-60))
    _refusal(store, token)


def test_every_unusable_continuation_yields_one_indistinguishable_outcome() -> None:
    """Five distinct causes, one caller-visible outcome."""
    engine = memory_engine()
    codec = HmacAuthorizedContinuationTokenCodec(KEY_ID, KEY)
    store = SqlAlchemyAuthorizedContinuationStore(engine, codec)
    live = store.persist(continuation_state())
    expired = store.persist(continuation_state(expires_in_seconds=-60, scan_limit=51))
    _, _, mac = live.partition(".")
    unknown_mac = base64.urlsafe_b64encode(bytes(range(32))).rstrip(b"=").decode()
    candidates = (
        "not-a-token",
        f"{KEY_ID}.{mac}=",
        f"other-key.{mac}",
        f"{KEY_ID}.{unknown_mac}",
        expired,
    )
    assert len(set(candidates)) == len(candidates)
    outcomes = {_refusal(store, candidate) for candidate in candidates}
    assert len(outcomes) == 1
    # And the live token still works, so "one outcome" is not "nothing works".
    assert store.claim_load(live).scan_limit == 50


# --- key lifecycle -----------------------------------------------------------


def test_a_restart_under_the_same_secret_store_version_preserves_tokens() -> None:
    engine = memory_engine()
    token = SqlAlchemyAuthorizedContinuationStore(
        engine, HmacAuthorizedContinuationTokenCodec(KEY_ID, KEY)
    ).persist(continuation_state())
    restarted = SqlAlchemyAuthorizedContinuationStore(
        engine, HmacAuthorizedContinuationTokenCodec(KEY_ID, KEY)
    )
    assert restarted.claim_load(token).scan_limit == 50


def test_a_new_key_under_the_same_key_id_invalidates_tokens_without_grace() -> None:
    """No previous-key grace: the drain runbook is the cutover, not a row CAS."""
    engine = memory_engine()
    token = SqlAlchemyAuthorizedContinuationStore(
        engine, HmacAuthorizedContinuationTokenCodec(KEY_ID, KEY)
    ).persist(continuation_state())
    rekeyed = SqlAlchemyAuthorizedContinuationStore(
        engine, HmacAuthorizedContinuationTokenCodec(KEY_ID, OTHER_KEY)
    )
    _refusal(rekeyed, token)
    assert rekeyed.row_lookup_count == 1


def test_a_new_key_id_invalidates_tokens_before_any_lookup() -> None:
    engine = memory_engine()
    token = SqlAlchemyAuthorizedContinuationStore(
        engine, HmacAuthorizedContinuationTokenCodec(KEY_ID, KEY)
    ).persist(continuation_state())
    rotated = SqlAlchemyAuthorizedContinuationStore(
        engine, HmacAuthorizedContinuationTokenCodec("pilot-k2", OTHER_KEY)
    )
    _refusal(rotated, token)
    assert rotated.row_lookup_count == 0


@pytest.mark.parametrize(
    ("key_id", "key"),
    [
        ("pilot-k1", b"too-short"),
        ("pilot k1", KEY),
        ("pilot.k1", KEY),
        ("", KEY),
    ],
)
def test_a_short_or_malformed_key_or_key_id_cannot_construct_a_codec(
    key_id: str, key: bytes
) -> None:
    with pytest.raises(ValueError):
        HmacAuthorizedContinuationTokenCodec(key_id, key)


def test_the_frozen_codec_version_is_bound_into_every_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Advancing the non-secret codec version invalidates tokens by construction."""
    codec = HmacAuthorizedContinuationTokenCodec(KEY_ID, KEY)
    state = continuation_state()
    before = codec.encode(state)
    monkeypatch.setattr(
        "ne.brain.current_source_authority.CONTINUATION_CODEC_VERSION",
        CONTINUATION_CODEC_VERSION + 1,
    )
    assert codec.encode(state) != before


# --- replay rows -------------------------------------------------------------


def _replay(token_mac: str, *, scanned: int) -> AuthorizedContinuationReplay:
    return AuthorizedContinuationReplay(
        token_mac=token_mac,
        state=continuation_state(),
        item_ids=(),
        status=AuthorizedArtifactPageStatus.COMPLETE,
        scanned_count=scanned,
        returned_count=0,
        scan_limit=50,
        contributor_stamps=(),
        acl_decision_fingerprints=(),
        scope_health_epoch=1,
        next_state_hash=None,
    )


def test_the_first_resolution_replay_row_is_immutable() -> None:
    """A divergent second resolution is DETECTED, never allowed to overwrite."""
    engine = memory_engine()
    codec = HmacAuthorizedContinuationTokenCodec(KEY_ID, KEY)
    store = SqlAlchemyAuthorizedContinuationStore(engine, codec)
    token_mac = codec.token_mac(continuation_state())
    first = _replay(token_mac, scanned=1)
    assert store.persist_replay(first) == first.replay_hash
    assert store.replay_matches(first)
    divergent = _replay(token_mac, scanned=2)
    assert divergent.replay_hash != first.replay_hash
    assert store.persist_replay(divergent) == first.replay_hash
    assert store.replay_matches(divergent) is False
