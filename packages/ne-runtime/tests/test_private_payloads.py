# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

import pytest

from ne.runtime import private_payloads
from ne.runtime.private_payloads import (
    AGENT_RUN_TURN_TARGET_KIND,
    CONVERSATION_TURN_TARGET_KIND,
    LEGACY_TARGET_KINDS,
    SESSION_MESSAGE_TARGET_KIND,
    TUPLED_TARGET_KINDS,
    CanonicalTargetTuple,
    KeyEnvelopeError,
    PrivatePayloadDomain,
    RunPrivatePayloadCipher,
    StaticPrivatePayloadKeys,
    conversation_turn_content_binding,
    is_claimable_target_kind,
    protected_target_hash,
    session_message_content_binding,
)


def _cipher(*, current: str = "k1") -> RunPrivatePayloadCipher:
    return RunPrivatePayloadCipher(
        StaticPrivatePayloadKeys(
            current_key_id=current,
            keys={"k1": b"1" * 32, "k2": b"2" * 32},
        )
    )


@pytest.mark.parametrize("domain", tuple(PrivatePayloadDomain))
def test_every_private_domain_requires_exact_target(domain: PrivatePayloadDomain) -> None:
    cipher = _cipher()
    binding = ("conversation", "run", "turn", "attempt")
    plaintext = f"private sentinel for {domain.value} — café".encode()
    row = cipher.encrypt_for_target(
        domain=domain,
        row_id="row-1",
        owner_principal_id="alice",
        binding=binding,
        plaintext=plaintext,
    )

    assert (
        cipher.materialize_for_target(
            row,
            domain=domain,
            row_id="row-1",
            owner_principal_id="alice",
            binding=binding,
        )
        == plaintext
    )
    assert plaintext not in row.payload_ciphertext.encode()

    for changed in (
        {
            "domain": PrivatePayloadDomain.RUN_INPUT
            if domain is not PrivatePayloadDomain.RUN_INPUT
            else PrivatePayloadDomain.EVIDENCE_LOCATOR
        },
        {"row_id": "row-2"},
        {"owner_principal_id": "bob"},
        {"binding": ("conversation", "run", "other-turn", "attempt")},
    ):
        kwargs = {
            "domain": domain,
            "row_id": "row-1",
            "owner_principal_id": "alice",
            "binding": binding,
            **changed,
        }
        with pytest.raises(KeyEnvelopeError, match="target mismatch"):
            cipher.materialize_for_target(row, **kwargs)


@pytest.mark.parametrize("field", ("payload_ciphertext", "encrypted_dek", "binding_hash"))
def test_ciphertext_tag_envelope_and_aad_tamper_fail_closed(field: str) -> None:
    cipher = _cipher()
    row = cipher.encrypt_for_target(
        domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
        row_id="turn-content-1",
        owner_principal_id="alice",
        binding=("conversation", "run", "turn"),
        plaintext=b"source-free user prompt sentinel",
    )
    current = getattr(row, field)
    replacement = ("0" if current[0] != "0" else "1") + current[1:]
    mutant = replace(row, **{field: replacement})

    with pytest.raises(
        (KeyEnvelopeError, ValueError), match=r"authentication|hash|base64url|target mismatch"
    ):
        cipher.materialize_for_target(
            mutant,
            domain=PrivatePayloadDomain.PROTECTED_TURN_CONTENT,
            row_id="turn-content-1",
            owner_principal_id="alice",
            binding=("conversation", "run", "turn"),
        )


def test_valid_same_owner_rows_cannot_swap_ciphertext() -> None:
    cipher = _cipher()
    first = cipher.encrypt_for_target(
        domain=PrivatePayloadDomain.PROTECTED_SOURCE_OUTPUT,
        row_id="output-a",
        owner_principal_id="alice",
        binding=("run", "turn-a", "call-a"),
        plaintext=b"output A sentinel",
    )
    second = cipher.encrypt_for_target(
        domain=PrivatePayloadDomain.PROTECTED_SOURCE_OUTPUT,
        row_id="output-b",
        owner_principal_id="alice",
        binding=("run", "turn-b", "call-b"),
        plaintext=b"output B sentinel",
    )
    swapped = replace(
        first,
        payload_nonce=second.payload_nonce,
        payload_ciphertext=second.payload_ciphertext,
        key_nonce=second.key_nonce,
        encrypted_dek=second.encrypted_dek,
    )
    with pytest.raises(KeyEnvelopeError, match="authentication failed"):
        cipher.materialize_for_target(
            swapped,
            domain=PrivatePayloadDomain.PROTECTED_SOURCE_OUTPUT,
            row_id="output-a",
            owner_principal_id="alice",
            binding=("run", "turn-a", "call-a"),
        )


def test_rewrap_changes_only_key_envelope_and_preserves_payload_bytes() -> None:
    cipher = _cipher()
    row = cipher.encrypt_for_target(
        domain=PrivatePayloadDomain.RUN_INPUT,
        row_id="input-1",
        owner_principal_id="alice",
        binding=("conversation", "run", "1"),
        plaintext=b"unique RunInput sentinel",
    )
    rotated = cipher.rewrap(row, target_key_id="k2")

    assert rotated.key_id == "k2"
    assert rotated.payload_nonce == row.payload_nonce
    assert rotated.payload_ciphertext == row.payload_ciphertext
    assert rotated.plaintext_hash == row.plaintext_hash
    assert (rotated.key_nonce, rotated.encrypted_dek) != (row.key_nonce, row.encrypted_dek)
    assert (
        cipher.materialize_for_target(
            rotated,
            domain=PrivatePayloadDomain.RUN_INPUT,
            row_id="input-1",
            owner_principal_id="alice",
            binding=("conversation", "run", "1"),
        )
        == b"unique RunInput sentinel"
    )


def test_missing_old_key_refuses_materialization_and_rewrap() -> None:
    original = _cipher().encrypt_for_target(
        domain=PrivatePayloadDomain.RUN_INPUT,
        row_id="input-1",
        owner_principal_id="alice",
        binding=("conversation", "run"),
        plaintext=b"secret",
    )
    without_old = RunPrivatePayloadCipher(
        StaticPrivatePayloadKeys(current_key_id="k2", keys={"k2": b"2" * 32})
    )
    with pytest.raises(KeyEnvelopeError, match="authentication failed"):
        without_old.materialize_for_target(
            original,
            domain=PrivatePayloadDomain.RUN_INPUT,
            row_id="input-1",
            owner_principal_id="alice",
            binding=("conversation", "run"),
        )
    with pytest.raises(KeyEnvelopeError, match="rewrap failed"):
        without_old.rewrap(original, target_key_id="k2")


# --- A1: the leaf now owns protected-row addressing ----------------------------


def _tuple(**overrides: object) -> CanonicalTargetTuple:
    base: dict[str, object] = {
        "conversation_id": "conv-1",
        "run_id": "run-1",
        "turn_id": "turn-1",
        "turn_sequence": 1,
        "attachment_id": "attach-1",
        "attempt_id": "attempt-1",
    }
    return CanonicalTargetTuple(**{**base, **overrides})  # type: ignore[arg-type]


def test_the_leaf_imports_neither_store() -> None:
    """private_payloads must stay a true leaf: stdlib + cryptography only.

    Both stores depend on it, so an import of either one here would create the
    cycle A1 exists to remove.
    """
    source = Path(private_payloads.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not {name for name in imported if name.startswith("ne.")}
    assert imported <= {
        "__future__",
        "base64",
        "binascii",
        "hashlib",
        "hmac",
        "json",
        "os",
        "collections.abc",
        "dataclasses",
        "enum",
        "typing",
        "cryptography.hazmat.primitives.ciphers.aead",
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("conversation_id", ""),
        ("run_id", "   "),
        ("turn_id", ""),
        ("attachment_id", ""),
        ("attempt_id", ""),
        ("turn_sequence", 0),
        ("turn_sequence", -1),
        ("turn_sequence", True),
        ("turn_sequence", "1"),
    ],
)
def test_canonical_target_tuple_rejects_an_incomplete_component(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        _tuple(**{field: value})
    # Positive control: the same tuple with a valid value constructs.
    assert _tuple() is not None


@pytest.mark.parametrize(
    "field",
    ["conversation_id", "run_id", "turn_id", "turn_sequence", "attachment_id", "attempt_id"],
)
def test_each_component_is_named_inside_the_binding(field: str) -> None:
    """Each label carries its OWN value, so neither a dropped nor a TRANSPOSED
    component can survive: presence alone would not notice two labels swapped.

    Every component is given a distinct value here for exactly that reason — with
    shared or defaulted values a transposition is unobservable by construction.
    """
    tuple_ = _tuple()
    binding = tuple_.binding()
    assert len(binding) == len(CanonicalTargetTuple.__dataclass_fields__)
    labelled = dict(item.split("=", 1) for item in binding)
    assert labelled == {name: str(value) for name, value in tuple_.as_columns().items()}
    assert labelled[field] == str(tuple_.as_columns()[field])
    value = 9 if field == "turn_sequence" else "changed"
    assert _tuple(**{field: value}).binding() != binding


def test_as_columns_is_content_free_and_matches_the_dataclass_fields() -> None:
    columns = _tuple().as_columns()
    assert set(columns) == set(CanonicalTargetTuple.__dataclass_fields__)
    assert all(isinstance(value, (str, int)) for value in columns.values())


def test_tupled_and_legacy_discriminators_are_disjoint_and_named() -> None:
    assert {CONVERSATION_TURN_TARGET_KIND, SESSION_MESSAGE_TARGET_KIND} == LEGACY_TARGET_KINDS
    assert {AGENT_RUN_TURN_TARGET_KIND} == TUPLED_TARGET_KINDS
    assert not (LEGACY_TARGET_KINDS & TUPLED_TARGET_KINDS)
    assert is_claimable_target_kind(AGENT_RUN_TURN_TARGET_KIND) is True
    for kind in LEGACY_TARGET_KINDS:
        assert is_claimable_target_kind(kind) is False
    with pytest.raises(ValueError):
        is_claimable_target_kind("")


def test_protected_target_hash_binds_domain_owner_kind_and_every_component() -> None:
    domain = PrivatePayloadDomain.PROTECTED_TURN_CONTENT
    other = PrivatePayloadDomain.PROTECTED_SOURCE_OUTPUT
    base = protected_target_hash(domain, "alice", AGENT_RUN_TURN_TARGET_KIND, _tuple().binding())
    assert base != protected_target_hash(
        other, "alice", AGENT_RUN_TURN_TARGET_KIND, _tuple().binding()
    )
    assert base != protected_target_hash(
        domain, "mallory", AGENT_RUN_TURN_TARGET_KIND, _tuple().binding()
    )
    assert base != protected_target_hash(
        domain, "alice", CONVERSATION_TURN_TARGET_KIND, _tuple().binding()
    )
    assert base == protected_target_hash(
        domain, "alice", AGENT_RUN_TURN_TARGET_KIND, _tuple().binding()
    )
    with pytest.raises(ValueError):
        protected_target_hash(domain, "alice", AGENT_RUN_TURN_TARGET_KIND, ())


def test_legacy_bindings_discriminate_their_own_components() -> None:
    a = conversation_turn_content_binding(
        conversation_id="c", attachment_id="a", attempt_id="t", slot="input"
    )
    b = conversation_turn_content_binding(
        conversation_id="c", attachment_id="a", attempt_id="t", slot="terminal"
    )
    assert a != b  # the two slots of ONE turn are different targets
    with pytest.raises(ValueError):
        conversation_turn_content_binding(
            conversation_id="c", attachment_id="a", attempt_id="t", slot="middle"
        )
    assert session_message_content_binding(
        session_id="s", sequence=1
    ) != session_message_content_binding(session_id="s", sequence=2)
    with pytest.raises(ValueError):
        session_message_content_binding(session_id="s", sequence=0)


def test_the_cipher_current_key_id_returns_the_id_not_the_key() -> None:
    """The readiness seam reads THIS, so it must be an identifier, not material.

    Scope, stated because the obvious stronger assertion is false: the cipher does
    hold its key resolver, and that resolver holds the KEKs — that is its job. What
    this pins is only that the newly exposed property returns the ID.
    """
    material = b"k" * 32
    cipher = RunPrivatePayloadCipher(
        StaticPrivatePayloadKeys(current_key_id="k1", keys={"k1": material})
    )
    assert cipher.current_key_id == "k1"
    assert isinstance(cipher.current_key_id, str)
    assert cipher.current_key_id.encode() != material
    # Positive control: it tracks the configured id rather than a constant.
    other = RunPrivatePayloadCipher(
        StaticPrivatePayloadKeys(current_key_id="k2", keys={"k2": material})
    )
    assert other.current_key_id == "k2"
