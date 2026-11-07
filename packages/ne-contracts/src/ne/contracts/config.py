# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Layered configuration framework and the process-start ``config_hash``.

SPEC section 4: ``config_hash`` is a SHA-256 over the fully-resolved,
canonicalized configuration tree (models, retrieval config, policy bundle
version, schema version, prompt versions), computed at process start and
stamped on every event and eval run. It is what makes "why did the system say
X on March 3" answerable.

``compute_config_hash`` hashes a *single* settings model. Because the platform
runs many module settings (``NeSettings`` subclasses) with no aggregate model,
the composed variant ``compute_platform_config_hash`` (added in 1.1.0, ADR-0003)
hashes a whole *mapping* of module settings — so the replay/audit hash covers
ALL module configs, not one. Module settings are PASSED IN (never imported),
keeping ``ne-contracts`` decoupled from every module (C5).

Secret masking in the preimage is two-layered: ``SecretStr``/``SecretBytes``
fields render as ``"**********"`` via ``model_dump``, AND — added by ADR-0006 —
URL *userinfo* (``scheme://user:pass@host``) embedded in ANY plain-``str`` value
is masked to a fixed sentinel before hashing, so DB passwords carried in
plain-``str`` DSN fields never enter the digest.

Layering precedence (HIGH -> LOW): init kwargs > environment (``NE_`` prefix,
``__`` nesting) > TOML file (``ne.toml`` by default) > field defaults.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections.abc import Mapping
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlsplit

from pydantic import BaseModel
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from ne.contracts.errors import (
    ConfigFileNotFoundError,
    ConfigHashMismatchError,
    ConfigHashNotFrozenError,
)

_explicit_toml_file: ContextVar[Path | None] = ContextVar("ne_explicit_toml_file", default=None)


class NeSettings(BaseSettings):
    """Base for every module's settings — layered pydantic-settings.

    Config-file selection (which file feeds the TOML layer):

    * **Static default** — subclasses may override ``toml_file`` in their
      ``model_config`` (defaults to ``ne.toml`` in the cwd).
    * **Per-instance / per-customer** — call
      ``Settings.from_config_file(path, **overrides)``; the selected file feeds
      the TOML layer for exactly that construction.
    * **Deployment convention** — set the ``NE_CONFIG_FILE`` environment
      variable; the base class honors it at construction time. An explicit
      ``from_config_file`` call always wins over ``NE_CONFIG_FILE``.

    Selection only changes *which* file feeds the TOML layer; value precedence is
    unchanged: init kwargs > environment (``NE_``/``__``) > TOML > field
    defaults. A selected-but-missing file fails fast with
    :class:`~ne.contracts.errors.ConfigFileNotFoundError` rather than silently
    falling back to defaults.

    ``config_file`` is a reserved, non-field name: subclasses must not declare a
    field called ``config_file`` (it is the ``from_config_file`` selector, not a
    setting). Selection is carried through a :class:`~contextvars.ContextVar`, so
    nested ``NeSettings`` constructions performed *inside* a ``from_config_file``
    call inherit the same file selection.
    """

    model_config = SettingsConfigDict(
        env_prefix="NE_",
        env_nested_delimiter="__",
        extra="forbid",
        toml_file=Path("ne.toml"),
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Order sources HIGH->LOW: init > env > TOML file > field defaults.

        pydantic-settings evaluates *earlier*-returned sources with *higher*
        precedence, so the returned tuple reads exactly as the precedence list.
        """
        return (
            init_settings,
            env_settings,
            cls._toml_source(settings_cls),
            file_secret_settings,
        )

    @staticmethod
    def _toml_source(settings_cls: type[BaseSettings]) -> TomlConfigSettingsSource:
        """Build the TOML source, honoring explicit selection then ``NE_CONFIG_FILE``.

        Precedence for *which file* feeds the TOML layer:

        1. an explicit ``from_config_file`` selection (the ContextVar), else
        2. the ``NE_CONFIG_FILE`` environment variable (empty string = unset;
           a set-but-missing path raises ``ConfigFileNotFoundError``), else
        3. the ``model_config`` ``toml_file`` default (``ne.toml`` in cwd).
        """
        explicit = _explicit_toml_file.get()
        if explicit is not None:
            return TomlConfigSettingsSource(settings_cls, toml_file=explicit)
        env_value = os.environ.get("NE_CONFIG_FILE")
        if env_value:
            path = Path(env_value)
            if not path.is_file():
                raise ConfigFileNotFoundError(
                    f"NE_CONFIG_FILE points to '{path}', which does not exist"
                )
            return TomlConfigSettingsSource(settings_cls, toml_file=path)
        return TomlConfigSettingsSource(settings_cls)

    @classmethod
    def from_config_file(cls, config_file: str | Path, /, **values: Any) -> Self:
        """Construct settings with ``config_file`` selected as the TOML layer source.

        ``config_file`` is not a model field — it selects which TOML file feeds
        the file layer for this one construction. Extra keyword ``values`` are
        passed straight to ``__init__`` as init-layer overrides (highest
        precedence). A missing file raises
        :class:`~ne.contracts.errors.ConfigFileNotFoundError`.
        """
        path = Path(config_file)
        if not path.is_file():
            raise ConfigFileNotFoundError(f"config file '{path}' does not exist")
        token = _explicit_toml_file.set(path)
        try:
            return cls(**values)
        finally:
            _explicit_toml_file.reset(token)


_USERINFO_SENTINEL = "***:***"
_PYDANTIC_SECRET_SENTINEL = "**********"
_SECRET_KEY_RE = re.compile(
    r"(?:(?:.+_)?(?:api_?key|access_?token|refresh_?token|bearer_?token|authorization|password|secret)|token)",
    re.IGNORECASE,
)
_RAW_ENV_KEYS = frozenset({"environ", "environment_variables", "raw_env", "raw_environment"})
# RFC 3986 scheme: ALPHA *( ALPHA / DIGIT / "+" / "-" / "." ). Used to confirm a
# valid scheme precedes the first "://" when ``urlsplit`` refuses to parse the
# rest of a malformed authority.
_URL_SCHEME_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.\-]*")


def _mask_userinfo_in_malformed_url(value: str) -> str:
    """Conservatively mask userinfo when ``urlsplit`` refuses to parse ``value``.

    ``urlsplit`` raises ``ValueError`` on some malformed authorities — e.g. an
    unterminated IPv6 literal such as ``postgresql://user:secret@[invalid/db``.
    A plain-``str`` DSN's password must NOT survive into the ``config_hash``
    preimage just because the DSN was syntactically odd (C12, bias =
    mask-more-never-leak). If a valid URI scheme precedes the first ``://``,
    treat the authority as the raw span from ``://`` up to the first ``/``,
    ``?`` or ``#`` and blank its userinfo (everything up to and including the
    final ``@``); otherwise (no valid scheme, e.g. prose) leave ``value``
    untouched, exactly as the parsed path does for non-URL strings.
    """
    prefix_len = value.index("://") + 3
    if not _URL_SCHEME_RE.fullmatch(value[: prefix_len - 3]):
        return value
    rest = value[prefix_len:]
    authority_len = len(rest)
    for i, ch in enumerate(rest):
        if ch in "/?#":
            authority_len = i
            break
    authority = rest[:authority_len]
    if "@" not in authority:
        return value
    hostpart = authority.rpartition("@")[2]
    netloc_end = prefix_len + authority_len
    return value[:prefix_len] + _USERINFO_SENTINEL + "@" + hostpart + value[netloc_end:]


def _mask_url_userinfo(value: str) -> str:
    """Mask the userinfo (``user:pass@``) of a whole-string URL value.

    A whole ``str`` that parses as ``scheme://user[:pass]@host...`` has its
    userinfo replaced with the fixed sentinel ``***:***`` (so both the password
    AND the — also sensitive, and rotation-relevant — username are removed),
    leaving the scheme (case preserved), host, port, path, query, and fragment
    byte-for-byte intact. This is what keeps a plain-``str`` DSN's embedded
    password out of the ``config_hash`` preimage (ADR-0006 / bulletproofing
    P1-1 / C12), and makes DSN credential rotation hash-invariant.

    Edge behavior (all decided in planning, bias = mask-more-never-leak):

    * ``postgresql+psycopg://user@host/db`` -> ``...://***:***@host/db``
      (user-only userinfo masked too — usernames are sensitive and rotation
      invariance applies uniformly).
    * ``redis://:pw@h`` -> ``redis://***:***@h`` (password-only userinfo).
    * A password containing an unencoded ``@`` (``user:p@ss@host``) is handled
      via ``rpartition("@")`` — only the final ``@`` separates userinfo from host.
    * Malformed authority that ``urlsplit`` refuses to parse (``ValueError`` —
      e.g. an unterminated IPv6 literal ``postgresql://u:p@[invalid/db``) is
      masked conservatively via :func:`_mask_userinfo_in_malformed_url` rather
      than left to leak (C12): the credential must never survive a syntactically
      odd DSN.
    * Untouched (returned unchanged): values with no ``://``; values with a
      ``://`` but no ``@`` in the netloc (``https://host/path``); ``sqlite:///x.db``
      (empty netloc, no userinfo); ``mailto:user@x`` (no netloc); scheme-relative
      ``//u:p@host`` (no scheme); prose containing invalid scheme characters
      (``foo bar://x:y@z``); the empty string.
    * LIMITATION: only a *whole-string* URL is masked. A URL embedded mid-prose,
      or a nested ``x://a://u:p@h``, is NOT masked — acceptable because config
      DSN fields are always whole-string values.
    """
    if "://" not in value:
        return value
    try:
        split = urlsplit(value)
    except ValueError:
        # ``urlsplit`` refuses some malformed authorities (e.g. an unterminated
        # IPv6 literal). We must still keep a plain-``str`` DSN's password out of
        # the preimage (C12), so fall back to a raw, conservative mask instead of
        # leaking the credential unchanged.
        return _mask_userinfo_in_malformed_url(value)
    if not split.scheme or "@" not in split.netloc:
        return value
    prefix_len = value.index("://") + 3
    # Confirm the first "://" is the scheme separator. urlsplit lowercases the
    # scheme but preserves its length, so compare lengths (never text) to avoid
    # masking a non-URL value whose "://" sits after arbitrary leading text.
    if len(split.scheme) != prefix_len - 3:
        return value
    netloc_end = prefix_len + len(split.netloc)
    # rpartition: defensive against an unencoded "@" inside the userinfo — only
    # the final "@" delimits userinfo from the host part.
    hostpart = split.netloc.rpartition("@")[2]
    return value[:prefix_len] + _USERINFO_SENTINEL + "@" + hostpart + value[netloc_end:]


def _mask_userinfo_tree(node: Any) -> Any:
    """Recursively mask URL userinfo in every string of a ``mode="json"`` dump.

    ``str`` -> :func:`_mask_url_userinfo`. ``dict`` -> keys AND values are masked
    (keys are always ``str`` in a ``mode="json"`` dump; masking keys too is
    defense-in-depth for configs keyed by a DSN — two keys differing only in
    userinfo would collide last-wins, which is pathological and accepted).
    ``list`` -> each element masked. Anything else (int/float/bool/None) is
    returned as-is.
    """
    if isinstance(node, str):
        return _mask_url_userinfo(node)
    if isinstance(node, dict):
        return {_mask_url_userinfo(k): _mask_userinfo_tree(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_mask_userinfo_tree(v) for v in node]
    return node


def _assert_artifact_safe(node: Any, *, path: str = "$") -> None:
    """Fail closed when resolved output still contains credential-shaped data."""
    if isinstance(node, dict):
        for key, value in node.items():
            normalized = key.casefold().replace("-", "_")
            child_path = f"{path}.{key}"
            if normalized in _RAW_ENV_KEYS:
                raise ValueError(f"raw environment projection is forbidden at {child_path}")
            if _SECRET_KEY_RE.fullmatch(normalized) and value not in (
                None,
                "",
                _PYDANTIC_SECRET_SENTINEL,
            ):
                raise ValueError(
                    f"unmasked secret-bearing configuration is forbidden at {child_path}"
                )
            _assert_artifact_safe(value, path=child_path)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _assert_artifact_safe(value, path=f"{path}[{index}]")


def _canonicalize_artifact_tree(node: Any) -> Any:
    """Return a recursively sorted JSON value, masking URL userinfo on the way."""
    masked = _mask_userinfo_tree(node)
    _assert_artifact_safe(masked)
    # This round trip both rejects non-JSON values/non-finite floats and returns
    # one detached, canonical JSON-safe value.  Never retain model/source metadata.
    encoded = json.dumps(
        masked,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return json.loads(encoded)


def _canonical_config_preimage(settings: BaseModel) -> str:
    """Return the canonical, userinfo-masked JSON preimage for a single model.

    Single source of truth for :func:`compute_config_hash` (and the guard
    tests): ``model_dump(mode="json")`` -> mask URL userinfo -> deterministic
    ``json.dumps`` (sorted keys, tight separators, ASCII).
    """
    masked = _mask_userinfo_tree(settings.model_dump(mode="json"))
    return json.dumps(masked, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _canonical_platform_preimage(modules: Mapping[str, BaseModel]) -> str:
    """Return the canonical, userinfo-masked JSON preimage for a module mapping.

    Single source of truth for :func:`compute_platform_config_hash`: each
    module's ``mode="json"`` dump is userinfo-masked, combined into
    ``{module_key: <masked tree>}``, then serialized deterministically.
    """
    return json.dumps(
        canonical_platform_config_tree(modules),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def canonical_platform_config_tree(modules: Mapping[str, BaseModel]) -> Any:
    """Return the canonical, masked, JSON-safe resolved platform config artifact.

    The returned value contains only module names and ``mode="json"`` model
    values. It excludes settings-source metadata and never reads ``os.environ``.
    Credential-shaped plain values and raw-environment projections fail closed.
    """
    if not modules:
        raise ValueError(
            "modules must not be empty: a platform config tree over zero module "
            "configs would describe an empty configuration"
        )
    if any(not isinstance(key, str) for key in modules):
        raise TypeError("platform config module keys must be strings")
    combined = {key: settings.model_dump(mode="json") for key, settings in modules.items()}
    return _canonicalize_artifact_tree(combined)


def compute_platform_config_tree_hash(tree: Any) -> str:
    """Hash a resolved config artifact using the platform's exact canonical JSON."""
    canonical = _canonicalize_artifact_tree(tree)
    if not isinstance(canonical, dict) or not canonical:
        raise ValueError("platform config tree must be a non-empty JSON object")
    preimage = json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(preimage.encode("utf-8")).hexdigest()


def compute_config_hash(settings: BaseModel) -> str:
    """Return the SHA-256 hex digest of the canonicalized config tree.

    The tree is serialized with ``model_dump(mode="json")``, which renders
    pydantic ``SecretStr`` / ``SecretBytes`` as ``"**********"``. Secret
    *material* therefore never enters the hash preimage, and secret *rotation*
    does not change the hash. The JSON is canonicalized (sorted keys, tight
    separators, ASCII) so the digest is independent of field/dict ordering.

    URL userinfo masking (ADR-0006). Before hashing, URL userinfo
    (``user:pass@``) in ANY string value is replaced with the fixed sentinel
    ``***:***``, so DB passwords carried in plain-``str`` DSN fields never enter
    the preimage and DSN credential rotation no longer changes the hash. This
    closes bulletproofing P1-1 / C12: previously an embedded Postgres password
    was an unsalted offline dictionary oracle in every event / U-P-B report /
    WORM export (the rest of the preimage being reconstructible from
    compose/values).

    Intended one-time change: any deployment whose config carried userinfo in a
    DSN gets a NEW ``config_hash`` on upgrade to this version; configs WITHOUT
    URL userinfo hash identically to before (byte-for-byte preimage preservation
    is tested). The reshuffle is scoped to userinfo-bearing configs only.
    """
    return hashlib.sha256(_canonical_config_preimage(settings).encode("utf-8")).hexdigest()


def compute_platform_config_hash(modules: Mapping[str, BaseModel]) -> str:
    """Return the SHA-256 hex digest over the canonicalized config of ALL modules.

    ``compute_config_hash`` hashes a single settings model; this composed variant
    (ADR-0003, audit #7) hashes a whole mapping of module settings so the SPEC
    section 4 replay/audit hash covers **every** module config, not one. Changing
    ANY module's config changes the platform hash.

    Determinism: each module's settings is canonicalized exactly as
    ``compute_config_hash`` does (``model_dump(mode="json")``, which renders
    ``SecretStr``/``SecretBytes`` as ``"**********"`` so secret material never
    enters the preimage and secret rotation does not change the hash; plus
    ADR-0006 URL-userinfo masking — ``user:pass@`` in ANY string value is
    replaced with the fixed sentinel ``***:***`` so plain-``str`` DSN passwords
    never enter the preimage and DSN credential rotation is hash-invariant). The
    ``{module_key: <canonical tree>}`` mapping is serialized with ``sort_keys``,
    which is recursive — so the digest is independent of BOTH the input
    mapping's insertion order AND every nested dict's key order. Every process
    freezing the same module mapping therefore computes the same hash.

    Intended one-time change (ADR-0006): any deployment whose module configs
    carried userinfo in a DSN gets a NEW platform ``config_hash`` on upgrade;
    module configs WITHOUT URL userinfo hash identically to before (byte-for-byte
    preimage preservation is tested).

    Modules are PASSED IN as ``Mapping[str, BaseModel]`` — ``ne-contracts`` never
    imports any module's settings, so the contract layer stays decoupled from
    every module (C5).

    The composed preimage ``{module_key: tree}`` is structurally distinct from
    any single-module preimage (a bare ``tree``), so the platform hash never
    collides with a constituent ``compute_config_hash`` (domain separation).

    Raises ``ValueError`` if ``modules`` is empty (a platform hash over zero
    module configs would stamp an empty configuration onto the replay hash).
    """
    return compute_platform_config_tree_hash(canonical_platform_config_tree(modules))


_frozen_hash: str | None = None
# Guards the check-and-set of ``_frozen_hash`` so a concurrent first-freeze race
# resolves to exactly one winner (the loser raises ConfigHashMismatchError).
_frozen_lock = threading.Lock()


def _freeze(computed: str) -> str:
    """Store ``computed`` as the process-global frozen config_hash and return it.

    Shared check-and-set core of ``freeze_config_hash`` and
    ``freeze_platform_config_hash``: guarded by the module-level lock, idempotent
    when ``computed`` equals the already-frozen hash, and raising
    :class:`ConfigHashMismatchError` on a divergent value. Two threads racing the
    first freeze with *different* digests resolve to exactly one winner.
    """
    global _frozen_hash
    with _frozen_lock:
        if _frozen_hash is not None and _frozen_hash != computed:
            raise ConfigHashMismatchError(
                f"config_hash already frozen as {_frozen_hash}; refused to re-freeze as {computed}"
            )
        _frozen_hash = computed
        return _frozen_hash


def freeze_config_hash(settings: BaseModel) -> str:
    """Compute, store, and return the process-start config_hash.

    Idempotent when re-called with a config that hashes identically; raises
    :class:`ConfigHashMismatchError` if a *different* hash is already frozen
    (configuration must not change mid-flight). The check-and-set is guarded by a
    module-level lock, so two threads racing the first freeze with *different*
    configs resolve to exactly one winner.
    """
    return _freeze(compute_config_hash(settings))


def freeze_platform_config_hash(modules: Mapping[str, BaseModel]) -> str:
    """Compute, store, and return the composed platform config_hash (ADR-0003).

    Freezes ``compute_platform_config_hash(modules)`` into the SAME
    process-global slot as :func:`freeze_config_hash` — there is exactly ONE
    frozen hash per process. Mixing a single-module freeze and a platform freeze
    whose digests differ therefore raises :class:`ConfigHashMismatchError`; the
    NE-033 composition root will freeze consistently via this platform variant.

    Idempotent when re-called with a mapping that hashes identically; raises
    :class:`ConfigHashMismatchError` on a divergent mapping. After a successful
    freeze, :func:`get_config_hash` returns this hash unchanged. Raises
    ``ValueError`` (via ``compute_platform_config_hash``) on an empty mapping,
    freezing nothing.
    """
    return _freeze(compute_platform_config_hash(modules))


def get_config_hash() -> str:
    """Return the frozen process-start config_hash.

    Raises :class:`ConfigHashNotFrozenError` if ``freeze_config_hash`` has not
    been called yet.
    """
    with _frozen_lock:
        if _frozen_hash is None:
            raise ConfigHashNotFrozenError(
                "config_hash has not been frozen; call freeze_config_hash() at process start"
            )
        return _frozen_hash


def reset_config_hash_for_testing() -> None:
    """Clear the frozen hash. Test-only — never call in production code."""
    global _frozen_hash
    with _frozen_lock:
        _frozen_hash = None
