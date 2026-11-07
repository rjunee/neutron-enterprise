# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""SecretStore interface and implementations."""

from __future__ import annotations

__version__ = "0.1.0"

from ne.secrets.codecs import PlainJsonCodec, SopsCodec
from ne.secrets.config import SecretsSettings
from ne.secrets.envfile import import_env_file, parse_env_file
from ne.secrets.errors import NeSecretsError, SecretsBackendError
from ne.secrets.factory import create_secret_store
from ne.secrets.file_store import FileSecretStore
from ne.secrets.openbao import OpenBaoSecretStore

__all__ = [
    "FileSecretStore",
    "NeSecretsError",
    "OpenBaoSecretStore",
    "PlainJsonCodec",
    "SecretsBackendError",
    "SecretsSettings",
    "SopsCodec",
    "__version__",
    "create_secret_store",
    "import_env_file",
    "parse_env_file",
]
