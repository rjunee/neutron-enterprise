# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Authenticated envelope encryption for Agent Run private payload rows.

The cipher deliberately knows only the seven storage domains owned by PRE-009A.
Callers must supply the exact target binding at both encryption and materialization;
there is no unscoped ``decrypt(row)`` convenience API.  Payload ciphertext and its
hash stay immutable while a quiesced key rotation rewraps only the per-row DEK.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

__all__ = [
    "AGENT_RUN_TURN_TARGET_KIND",
    "CONVERSATION_TURN_TARGET_KIND",
    "LEGACY_TARGET_KINDS",
    "SESSION_MESSAGE_TARGET_KIND",
    "TUPLED_TARGET_KINDS",
    "CanonicalTargetTuple",
    "KeyEnvelopeError",
    "PrivatePayloadDomain",
    "PrivatePayloadEnvelope",
    "ProtectedContentStatus",
    "RunPrivatePayloadCipher",
    "StaticPrivatePayloadKeys",
    "conversation_turn_content_binding",
    "decode_private_payload_b64",
    "is_claimable_target_kind",
    "protected_payload_aad_binding",
    "protected_target_hash",
    "session_message_content_binding",
]


class KeyEnvelopeError(RuntimeError):
    """A private row cannot be authenticated under its exact stored binding."""


class PrivatePayloadDomain(StrEnum):
    """The complete, closed PRE-009A encrypted-row domain set."""

    RUN_INPUT = "run_input"
    EVIDENCE_LOCATOR = "evidence_locator"
    PENDING_SOURCE_TOOL_RESULT = "pending_source_tool_result"
    PROTECTED_TURN_CONTENT = "protected_turn_content"
    PROTECTED_SOURCE_OUTPUT = "protected_source_output"
    PROTECTED_SOURCE_INVOCATION = "protected_source_invocation"
    PROTECTED_WORKFLOW_STATE = "protected_workflow_state"


# --- Protected-row addressing (the LEAF derivation) ---------------------------
#
# This lives below both stores so the claim path, the RunClaim binding and the
# legacy migration verifier all call ONE implementation. A second spelling above
# would let a cross-target reference satisfy whichever copy it happened to reach.
# The module stays stdlib + cryptography and imports neither store.

#: The one TUPLED discriminator: a row under this kind carries the complete
#: canonical target tuple, and it is the only kind a claim slice may resolve through.
AGENT_RUN_TURN_TARGET_KIND = "agent_run_turn"
TUPLED_TARGET_KINDS = frozenset({AGENT_RUN_TURN_TARGET_KIND})

#: Legacy discriminators: pre-Run plaintext with no canonical target tuple. They
#: stay addressable so migration can reach them, and NONCLAIMABLE so no claim slice
#: can resolve through one. Distinctness is asserted, not left to reading order.
CONVERSATION_TURN_TARGET_KIND = "conversation_turn"
SESSION_MESSAGE_TARGET_KIND = "session_message"
LEGACY_TARGET_KINDS = frozenset({CONVERSATION_TURN_TARGET_KIND, SESSION_MESSAGE_TARGET_KIND})

assert len(LEGACY_TARGET_KINDS) == 2, "the legacy discriminators must stay distinct"
assert not (LEGACY_TARGET_KINDS & TUPLED_TARGET_KINDS), "a legacy kind is never claimable"


def is_claimable_target_kind(target_kind: str) -> bool:
    """Whether a claim slice may address this discriminator at all.

    Claimable means TUPLED, not merely "not legacy". The other Run-scoped kinds —
    tool-call results, approval arguments, workflow state — are addressed by their
    own bindings and carry no canonical turn tuple, so a claim slice must not reach
    them either. Defining this as ``not legacy`` would silently make every one of
    them claim-addressable the day it was added.
    """
    if not target_kind or not target_kind.strip():
        raise ValueError("protected payload target kind must be non-empty")
    return target_kind in TUPLED_TARGET_KINDS


class ProtectedContentStatus(StrEnum):
    """The CLOSED, authenticated content discriminator for ProtectedTurnContent.

    SPEC binds ``content_status=source_free|source_bound_container|legacy_unavailable``
    into the versioned AAD. It is authenticated rather than merely stored, because a
    model or client must not be able to declare content source-free: a source-bearing
    row presented as source-free is exactly the disclosure this store exists to
    prevent, and a status that is only a column can be edited without invalidating
    the ciphertext that it describes.

    This is the SAME vocabulary the legacy-plaintext migration declares, deliberately:
    one closed set, so a migration cannot declare a status the protected row does not
    authenticate.
    """

    SOURCE_FREE = "source_free"
    SOURCE_BOUND_CONTAINER = "source_bound_container"
    LEGACY_UNAVAILABLE = "legacy_unavailable"

    @property
    def carries_source_bytes(self) -> bool:
        """Whether rows of this status may hold source-derived parts."""
        return self is ProtectedContentStatus.SOURCE_BOUND_CONTAINER


def protected_payload_aad_binding(
    *,
    target_kind: str,
    target_binding: Sequence[str],
    content_status: ProtectedContentStatus | None,
) -> tuple[str, ...]:
    """The versioned AAD binding for one protected row.

    ``content_status`` participates in the AAD but NOT in ``protected_target_hash``.
    That split is deliberate and is the whole reconciliation: the ADDRESS must stay
    stable (a status is a property of what a row holds, and folding a reclassifiable
    value into a row's identity would silently re-point every stored reference to
    it), while the AAD must authenticate the status (so the bytes cannot be reused
    under a different claim about what they are). Bind it, persist it, derive from
    it -- without making it a component of identity.
    """
    if not target_kind or not target_kind.strip():
        raise ValueError("protected payload target kind must be non-empty")
    if not target_binding or any(not value for value in target_binding):
        raise ValueError("protected payload target binding must be complete")
    if content_status is None:
        return (target_kind, *target_binding)
    return (target_kind, *target_binding, f"content_status={content_status.value}")


def _require_nonempty(**values: str) -> None:
    for name, value in values.items():
        if not value or not value.strip():
            raise ValueError(f"{name} must be non-empty")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class CanonicalTargetTuple:
    """The exact, CONTENT-FREE address of one Agent Run turn's protected row.

    Every component is an identifier or an ordinal; nothing here carries prompt,
    output, citation or claim text. That is what lets the tuple be stored in the
    clear beside the ciphertext and be QUERIED. Storing only ``target_hash`` would
    leave a row unable to say which turn it belongs to without being decrypted
    first, and a resolver that must decrypt in order to decide whether it may
    decrypt is not a resolver.
    """

    conversation_id: str
    run_id: str
    turn_id: str
    turn_sequence: int
    attachment_id: str
    attempt_id: str

    def __post_init__(self) -> None:
        _require_nonempty(
            conversation_id=self.conversation_id,
            run_id=self.run_id,
            turn_id=self.turn_id,
            attachment_id=self.attachment_id,
            attempt_id=self.attempt_id,
        )
        if isinstance(self.turn_sequence, bool) or not isinstance(self.turn_sequence, int):
            raise ValueError("turn_sequence must be an integer ordinal")
        if self.turn_sequence < 1:
            raise ValueError("turn_sequence must be a positive ordinal")

    def binding(self) -> tuple[str, ...]:
        """Self-describing address components, in a fixed order.

        Each component carries its own field name, so dropping one changes the
        address for every caller instead of silently colliding two targets whose
        remaining values happen to line up positionally.
        """
        return (
            f"conversation_id={self.conversation_id}",
            f"run_id={self.run_id}",
            f"turn_id={self.turn_id}",
            f"turn_sequence={self.turn_sequence}",
            f"attachment_id={self.attachment_id}",
            f"attempt_id={self.attempt_id}",
        )

    def as_columns(self) -> dict[str, object]:
        """The persisted content-free columns for this tuple."""
        return {
            "conversation_id": self.conversation_id,
            "run_id": self.run_id,
            "turn_id": self.turn_id,
            "turn_sequence": self.turn_sequence,
            "attachment_id": self.attachment_id,
            "attempt_id": self.attempt_id,
        }


def protected_target_hash(
    domain: PrivatePayloadDomain,
    owner_principal_id: str,
    target_kind: str,
    target_binding: Sequence[str],
) -> str:
    """The single derivation of a protected row's exact target address."""
    if not target_binding or any(not value for value in target_binding):
        raise ValueError("protected payload target binding must be complete")
    _require_nonempty(owner_principal_id=owner_principal_id, target_kind=target_kind)
    return hashlib.sha256(
        _canonical_json([domain.value, owner_principal_id, target_kind, *target_binding]).encode(
            "utf-8"
        )
    ).hexdigest()


def conversation_turn_content_binding(
    *, conversation_id: str, attachment_id: str, attempt_id: str, slot: str
) -> tuple[str, ...]:
    """The exact protected-row binding for one LEGACY Conversation-turn slot.

    The two slots of one turn are DIFFERENT targets: without the slot component a
    terminal-output row would address the same target as its own input row.
    """
    if slot not in ("input", "terminal"):
        raise ValueError("Conversation turn protected slot is invalid")
    _require_nonempty(
        conversation_id=conversation_id, attachment_id=attachment_id, attempt_id=attempt_id
    )
    return (conversation_id, attachment_id, attempt_id, slot)


def session_message_content_binding(*, session_id: str, sequence: int) -> tuple[str, ...]:
    """The exact protected-row binding for one LEGACY session message."""
    _require_nonempty(session_id=session_id)
    if sequence < 1:
        raise ValueError("session message sequence must be positive")
    return (session_id, str(sequence))


class PrivatePayloadKeys(Protocol):
    """Resolve deployment-local KEKs without exposing them to stored config."""

    @property
    def current_key_id(self) -> str: ...

    def resolve(self, key_id: str) -> bytes: ...


@dataclass(frozen=True)
class StaticPrivatePayloadKeys:
    """Small exact key resolver used by composition and conformance tests."""

    current_key_id: str
    keys: dict[str, bytes]

    def __post_init__(self) -> None:
        if not self.current_key_id or self.current_key_id not in self.keys:
            raise ValueError("current private-payload key id must resolve")
        if any(not key_id or len(kek) != 32 for key_id, kek in self.keys.items()):
            raise ValueError("private-payload KEKs must be named 256-bit values")

    def resolve(self, key_id: str) -> bytes:
        try:
            return self.keys[key_id]
        except KeyError as exc:
            raise KeyEnvelopeError("private payload key unavailable") from exc


@dataclass(frozen=True)
class PrivatePayloadEnvelope:
    """Only authenticated ciphertext metadata persisted beside one private row."""

    cipher_version: int
    domain: PrivatePayloadDomain
    row_id: str
    owner_principal_id: str
    binding_hash: str
    plaintext_hash: str
    key_id: str
    payload_nonce: str
    payload_ciphertext: str
    key_nonce: str
    encrypted_dek: str

    def __post_init__(self) -> None:
        if self.cipher_version != 1:
            raise ValueError("unsupported private-payload cipher version")
        if not self.row_id or not self.owner_principal_id or not self.key_id:
            raise ValueError("private-payload identity fields must be non-empty")
        for value in (self.binding_hash, self.plaintext_hash):
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError("private-payload hashes must be lowercase sha256")
        if len(_decode(self.payload_nonce)) != 12 or len(_decode(self.key_nonce)) != 12:
            raise ValueError("AES-GCM nonces must be 96-bit")
        if len(_decode(self.encrypted_dek)) != 48:
            raise ValueError("wrapped 256-bit DEK must include one AES-GCM tag")
        if len(_decode(self.payload_ciphertext)) < 16:
            raise ValueError("payload ciphertext must include an AES-GCM tag")


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    if not value or "=" in value:
        raise ValueError("private-payload base64url must be canonical and unpadded")
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("invalid private-payload base64url") from exc
    if _encode(decoded) != value:
        raise ValueError("non-canonical private-payload base64url")
    return decoded


def decode_private_payload_b64(value: str) -> bytes:
    """Public name for the ONE canonical unpadded-base64url decoder.

    Composition (`ne.api.platform`) parses the ephemeral KEK from the environment
    and must apply exactly the spelling rules the envelope fields already use --
    padding rejected, `-_` alphabet, round-trip checked. Exporting the existing
    implementation keeps that a single derivation; a second decoder in the API
    package is precisely how a key comes to be written one way and read another.
    """
    return _decode(value)


def _field(value: str) -> bytes:
    encoded = value.encode("utf-8")
    return len(encoded).to_bytes(8, "big") + encoded


def _binding_bytes(binding: tuple[str, ...]) -> bytes:
    if not binding or any(not value for value in binding):
        raise ValueError("private-payload target binding must be complete")
    return b"".join((_field("ne.agent-private-payload-binding.v1"), *(_field(v) for v in binding)))


def _payload_aad(
    *,
    domain: PrivatePayloadDomain,
    row_id: str,
    owner_principal_id: str,
    plaintext_hash: str,
    binding_hash: str,
) -> bytes:
    return b"".join(
        _field(value)
        for value in (
            "ne.agent-private-payload.v1",
            domain.value,
            row_id,
            owner_principal_id,
            plaintext_hash,
            binding_hash,
        )
    )


def _key_aad(
    *,
    domain: PrivatePayloadDomain,
    row_id: str,
    owner_principal_id: str,
    plaintext_hash: str,
    binding_hash: str,
    key_id: str,
) -> bytes:
    return b"".join(
        _field(value)
        for value in (
            "ne.agent-private-payload-key-envelope.v1",
            domain.value,
            row_id,
            owner_principal_id,
            plaintext_hash,
            binding_hash,
            key_id,
        )
    )


class RunPrivatePayloadCipher:
    """Encrypt, exact-target materialize, and rewrap PRE-009A private rows."""

    def __init__(self, keys: PrivatePayloadKeys) -> None:
        self._keys = keys

    @property
    def current_key_id(self) -> str:
        """The key id new rows are wrapped under. Never the key material itself."""
        return self._keys.current_key_id

    def encrypt_for_target(
        self,
        *,
        domain: PrivatePayloadDomain,
        row_id: str,
        owner_principal_id: str,
        binding: tuple[str, ...],
        plaintext: bytes,
    ) -> PrivatePayloadEnvelope:
        if not row_id or not owner_principal_id:
            raise ValueError("private-payload row and owner must be non-empty")
        binding_hash = hashlib.sha256(_binding_bytes(binding)).hexdigest()
        plaintext_hash = hashlib.sha256(plaintext).hexdigest()
        key_id = self._keys.current_key_id
        kek = self._keys.resolve(key_id)
        dek = os.urandom(32)
        payload_nonce = os.urandom(12)
        payload_ciphertext = AESGCM(dek).encrypt(
            payload_nonce,
            plaintext,
            _payload_aad(
                domain=domain,
                row_id=row_id,
                owner_principal_id=owner_principal_id,
                plaintext_hash=plaintext_hash,
                binding_hash=binding_hash,
            ),
        )
        key_nonce = os.urandom(12)
        encrypted_dek = AESGCM(kek).encrypt(
            key_nonce,
            dek,
            _key_aad(
                domain=domain,
                row_id=row_id,
                owner_principal_id=owner_principal_id,
                plaintext_hash=plaintext_hash,
                binding_hash=binding_hash,
                key_id=key_id,
            ),
        )
        return PrivatePayloadEnvelope(
            cipher_version=1,
            domain=domain,
            row_id=row_id,
            owner_principal_id=owner_principal_id,
            binding_hash=binding_hash,
            plaintext_hash=plaintext_hash,
            key_id=key_id,
            payload_nonce=_encode(payload_nonce),
            payload_ciphertext=_encode(payload_ciphertext),
            key_nonce=_encode(key_nonce),
            encrypted_dek=_encode(encrypted_dek),
        )

    def materialize_for_target(
        self,
        envelope: PrivatePayloadEnvelope,
        *,
        domain: PrivatePayloadDomain,
        row_id: str,
        owner_principal_id: str,
        binding: tuple[str, ...],
    ) -> bytes:
        expected_binding_hash = hashlib.sha256(_binding_bytes(binding)).hexdigest()
        if not (
            envelope.domain is domain
            and hmac.compare_digest(envelope.row_id, row_id)
            and hmac.compare_digest(envelope.owner_principal_id, owner_principal_id)
            and hmac.compare_digest(envelope.binding_hash, expected_binding_hash)
        ):
            raise KeyEnvelopeError("private payload target mismatch")
        try:
            kek = self._keys.resolve(envelope.key_id)
            dek = AESGCM(kek).decrypt(
                _decode(envelope.key_nonce),
                _decode(envelope.encrypted_dek),
                _key_aad(
                    domain=envelope.domain,
                    row_id=envelope.row_id,
                    owner_principal_id=envelope.owner_principal_id,
                    plaintext_hash=envelope.plaintext_hash,
                    binding_hash=envelope.binding_hash,
                    key_id=envelope.key_id,
                ),
            )
            plaintext = AESGCM(dek).decrypt(
                _decode(envelope.payload_nonce),
                _decode(envelope.payload_ciphertext),
                _payload_aad(
                    domain=envelope.domain,
                    row_id=envelope.row_id,
                    owner_principal_id=envelope.owner_principal_id,
                    plaintext_hash=envelope.plaintext_hash,
                    binding_hash=envelope.binding_hash,
                ),
            )
        except Exception as exc:
            raise KeyEnvelopeError("private payload authentication failed") from exc
        if not hmac.compare_digest(hashlib.sha256(plaintext).hexdigest(), envelope.plaintext_hash):
            raise KeyEnvelopeError("private payload authentication failed")
        return plaintext

    def rewrap(
        self,
        envelope: PrivatePayloadEnvelope,
        *,
        target_key_id: str,
    ) -> PrivatePayloadEnvelope:
        """Rewrap one DEK; payload nonce/ciphertext/hash remain byte-identical."""
        if target_key_id == envelope.key_id:
            return envelope
        try:
            old_kek = self._keys.resolve(envelope.key_id)
            dek = AESGCM(old_kek).decrypt(
                _decode(envelope.key_nonce),
                _decode(envelope.encrypted_dek),
                _key_aad(
                    domain=envelope.domain,
                    row_id=envelope.row_id,
                    owner_principal_id=envelope.owner_principal_id,
                    plaintext_hash=envelope.plaintext_hash,
                    binding_hash=envelope.binding_hash,
                    key_id=envelope.key_id,
                ),
            )
            new_kek = self._keys.resolve(target_key_id)
            nonce = os.urandom(12)
            wrapped = AESGCM(new_kek).encrypt(
                nonce,
                dek,
                _key_aad(
                    domain=envelope.domain,
                    row_id=envelope.row_id,
                    owner_principal_id=envelope.owner_principal_id,
                    plaintext_hash=envelope.plaintext_hash,
                    binding_hash=envelope.binding_hash,
                    key_id=target_key_id,
                ),
            )
        except Exception as exc:
            raise KeyEnvelopeError("private payload key rewrap failed") from exc
        return PrivatePayloadEnvelope(
            cipher_version=envelope.cipher_version,
            domain=envelope.domain,
            row_id=envelope.row_id,
            owner_principal_id=envelope.owner_principal_id,
            binding_hash=envelope.binding_hash,
            plaintext_hash=envelope.plaintext_hash,
            key_id=target_key_id,
            payload_nonce=envelope.payload_nonce,
            payload_ciphertext=envelope.payload_ciphertext,
            key_nonce=_encode(nonce),
            encrypted_dek=_encode(wrapped),
        )

    @staticmethod
    def canonical_json(value: object) -> bytes:
        """Encode bounded storage payloads without lossy alternate representations."""
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
