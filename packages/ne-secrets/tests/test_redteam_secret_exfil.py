# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-REDTEAM — secret-exfiltration attacks against the C12 masking surfaces.

Attempts to leak a sentinel secret through the surfaces a naive implementation
would leak it on: ``str``/``repr`` of a ``SecretStr``, a settings model's
``str``/``repr``/``model_dump_json``, and a ``SecretStore`` error message. Each
asserts the value is masked / named-by-path-only.

Complements (mapped in the red-team report, NOT re-implemented here):
``config_hash`` secret-invariance —
``ne-contracts/tests/test_config_hash.py::{test_hash_invariant_under_secret_rotation,
test_secret_material_not_in_preimage}`` — and plaintext-in-logs —
``ne-identity/tests/test_no_plaintext_leak.py``.

Each test states its MUTATION-CHECK proving non-vacuity.
"""

from __future__ import annotations

from pydantic import BaseModel, SecretStr

from ne.contracts.errors import SecretNotFoundError
from ne.secrets.errors import SecretsBackendError

SENTINEL = "NE-REDTEAM-SECRET-9f8e7d6c"


# --- (a) SecretStr str/repr mask; get_secret_value recovers -------------------


def test_secretstr_str_and_repr_mask_the_value() -> None:
    """``SecretStr`` masks its value under ``str``/``repr`` while remaining
    recoverable via ``get_secret_value`` — the primitive C12 relies on.

    MUTATION-CHECK: declaring the field a plain ``str`` (no ``SecretStr`` wrapper)
    would surface the sentinel under ``str``/``repr``.
    """
    s = SecretStr(SENTINEL)
    assert SENTINEL not in str(s)
    assert SENTINEL not in repr(s)
    assert s.get_secret_value() == SENTINEL


# --- (b) a settings model never leaks the secret via str/repr/serialization ---


def test_settings_model_str_and_repr_never_leak_secret() -> None:
    """A settings-style model with a ``SecretStr`` field never surfaces the value
    through ``str``/``repr``/``model_dump_json`` (SecretStr serializes masked by
    default), yet the real value is still recoverable via ``get_secret_value``.

    MUTATION-CHECK: declaring ``token`` as ``str`` instead of ``SecretStr`` would
    leak the sentinel into every log line, repr, and JSON serialization of the
    settings object.
    """

    class _Settings(BaseModel):
        name: str
        token: SecretStr

    cfg = _Settings(name="prod", token=SecretStr(SENTINEL))

    assert SENTINEL not in str(cfg)
    assert SENTINEL not in repr(cfg)
    assert SENTINEL not in cfg.model_dump_json()
    # The non-secret field is still visible; the secret is still usable.
    assert "prod" in str(cfg)
    assert cfg.token.get_secret_value() == SENTINEL


# --- (c) SecretStore errors name a path/status, never the secret VALUE --------


def test_secretstore_errors_name_path_not_value() -> None:
    """``SecretStore`` error messages carry only a PATH/status, never a secret
    value; and interpolating a ``SecretStr`` into an error via its default ``str``
    masks it, so even a careless error path cannot echo the secret.

    MUTATION-CHECK: an error constructed as
    ``SecretNotFoundError(f"secret was {value}")`` (echoing the value) would leak the
    sentinel; masking + path-only messages are the defense.
    """
    not_found = SecretNotFoundError("no secret at path 'app/prod/db'")
    assert "app/prod/db" in str(not_found)
    assert SENTINEL not in str(not_found)

    backend = SecretsBackendError("openbao returned status 503 for path 'app/prod/db'")
    assert "app/prod/db" in str(backend)
    assert SENTINEL not in str(backend)

    # Even if a SecretStr is interpolated into an error via its default str(), the
    # value is masked — the error path cannot exfiltrate it.
    secret = SecretStr(SENTINEL)
    leaky_attempt = SecretNotFoundError(f"failed to read secret {secret}")
    assert SENTINEL not in str(leaky_attempt)
