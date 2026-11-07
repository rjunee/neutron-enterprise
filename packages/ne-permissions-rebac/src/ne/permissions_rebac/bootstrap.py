# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Store/model bootstrap + fail-closed model-pin verification (NE-087 §4.4).

The authorization model is stored as package data at
``ne/permissions_rebac/model/neutron.model.json`` (ships in the wheel; the
human-canonical ``neutron.fga`` mirror lives beside it). :func:`bootstrap`
creates a store and writes the model (used live only at O-4); every process start
then calls :func:`verify_model_pin` (O-4 wiring) so a drifted/edited server-side
model fails closed BEFORE any check is trusted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections.abc import Callable
from importlib import resources
from typing import Any, Protocol

from ne.permissions_rebac.config import RebacSettings
from ne.permissions_rebac.errors import ModelPinError
from ne.permissions_rebac.ports import OpenFgaTupleClient
from ne.permissions_rebac.sovereignty import validate_openfga_url

__all__ = ["bootstrap", "load_model", "main", "verify_model_pin"]


class _BootstrapSurface(Protocol):
    """The narrow un-pinned surface the operator CLI needs (see ``_client``)."""

    async def create_store(self, name: str) -> str: ...

    async def write_authorization_model(self, model: Any) -> str: ...

    async def close(self) -> None: ...


_COMPARABLE_KEYS = ("schema_version", "type_definitions", "conditions")


def load_model() -> dict[str, Any]:
    """Load the pinned authorization model dict from package data."""
    text = (
        resources.files("ne.permissions_rebac")
        .joinpath("model/neutron.model.json")
        .read_text(encoding="utf-8")
    )
    model: dict[str, Any] = json.loads(text)
    return model


def _canonical(obj: Any) -> Any:
    """Order-independent canonical form: dict keys sorted, lists sorted by content."""
    if isinstance(obj, dict):
        return {k: _canonical(obj[k]) for k in sorted(obj)}
    if isinstance(obj, list):
        return sorted((_canonical(x) for x in obj), key=lambda e: json.dumps(e, sort_keys=True))
    return obj


#: The exhaustive set of OpenFGA AuthorizationModel field names where presence of
#: an EMPTY object (raw ``{}``) is itself the semantic signal, not an unset default.
#: Within the AuthorizationModel schema surface (as opposed to unrelated Check /
#: ListObjects / ListUsers request types, which ``_comparable`` never touches),
#: only ``Userset.this`` and ``RelationReference.wildcard`` are typed ``object``
#: with no sub-fields of their own. Every other zero-field-when-set OpenAPI type
#: this comparison ever encounters (``TypeDefinition.relations``,
#: ``AuthorizationModel.conditions``, ``Metadata.relations``, …) is a MAP, whose
#: empty state is semantically identical to the field being absent (OBSERVED on
#: the real server: a type with no relations round-trips as ``"relations": {}``,
#: and a model with no conditions round-trips as top-level ``"conditions": {}"``)
#: — so those strip like any other unset default.
#: Exhaustiveness is LOCKED, not argued: ``test_discovered_markers_equal_presence_marker_keys``
#: (``tests/test_presence_marker_exhaustive.py``) walks ``openapi_types`` transitively
#: from ``AuthorizationModel`` at test time, reflection-discovers every zero-field
#: marker-object field the installed SDK actually declares, and asserts that
#: discovered set equals this constant — so an SDK bump that adds or removes such a
#: field fails the suite instead of silently rotting this comment's claim.
_PRESENCE_MARKER_KEYS = frozenset({"this", "wildcard"})


def _without_unset_defaults(obj: Any) -> Any:
    """Recursively drop dict entries whose RAW value is an unset default.

    The OpenFGA SDK's generated ``to_dict(serialize=True)`` emits EVERY declared
    OpenAPI field of every nested object — unset scalar/list fields as ``None`` (or,
    off the real server's proto3 JSON, ``""`` for strings), and unset MAP fields
    (``relations``, ``metadata``, ``conditions``, …) as EITHER ``None`` or a raw
    empty ``{}`` depending on the exact call path (OBSERVED: the local
    ``ApiClient.__deserialize`` round-trip in ``test_bootstrap.py::_sdk_roundtrip``
    yields ``None`` for these; the REAL live server instead emits raw ``{}`` for
    the very same "no relations" / "no conditions" state) — while the in-repo
    ``neutron.model.json`` is written sparse (finding NE-LP-REBAC-D2). Dropping
    raw ``None``/``""``/``[]``, and raw ``{}`` EXCEPT at the two presence-marker
    keys in :data:`_PRESENCE_MARKER_KEYS`, is safe because those are exactly and
    only how "field not set (or set-but-empty, which OpenFGA treats identically
    for map-typed fields)" is represented; no OTHER field names are hardcoded, so
    an SDK version bump that changes WHICH fields it injects cannot rot this.

    The two presence-marker keys are the exception: in OpenFGA, presence is
    semantic for exactly two zero-field object types — ``Userset.this``
    (``{"this": {}}`` = direct-userset rewrite) and ``RelationReference.wildcard``
    (``{"type": "user", "wildcard": {}}`` = ``user:*``, PUBLIC access). Both must
    compare unequal to their absence, or an added ``relation: {"this": {}}`` /
    widened ``wildcard: {}`` drift would silently compare equal to its absence —
    exactly the vacuity a blanket "strip every raw {}" rule introduced (a review
    finding on an earlier version of this fix: ``wildcard`` sits as a SIBLING of
    ``type``/``relation``/``condition`` inside the SAME dict, so — unlike ``this``,
    which happens to sit inside a container keyed by relation name — no accidental
    "parent survives" protection covers it). Locked by
    ``test_verify_raises_on_wildcard_widening_even_after_sdk_round_trip`` (fake
    lane) and ``test_live_model_pin_rejects_wildcard_widening`` (real server).
    The exhaustiveness of :data:`_PRESENCE_MARKER_KEYS` itself — i.e. that no
    THIRD zero-field marker exists in the installed SDK — is locked by
    ``test_discovered_markers_equal_presence_marker_keys``
    (``tests/test_presence_marker_exhaustive.py``).
    ``False``/``0`` are deliberately kept (not unset defaults).
    Applied SYMMETRICALLY to both server and in-repo model via ``_comparable``.
    """
    if isinstance(obj, dict):
        return {
            k: _without_unset_defaults(v)
            for k, v in obj.items()
            if not (v is None or v == "" or v == [] or (v == {} and k not in _PRESENCE_MARKER_KEYS))
        }
    if isinstance(obj, list):
        return [_without_unset_defaults(v) for v in obj]
    return obj


def _comparable(model: dict[str, Any]) -> Any:
    """The pin-relevant subset (drops server-added ``id`` etc.), stripped + canonicalized."""
    subset = {k: model[k] for k in _COMPARABLE_KEYS if k in model}
    return _canonical(_without_unset_defaults(subset))


async def bootstrap(client: OpenFgaTupleClient, name: str = "neutron") -> tuple[str, str]:
    """Create the store and write the pinned model; return ``(store_id, model_id)``."""
    store_id = await client.create_store(name)
    model_id = await client.write_authorization_model(load_model())
    return store_id, model_id


async def verify_model_pin(client: OpenFgaTupleClient, settings: RebacSettings) -> None:
    """Fail closed unless the server's pinned model equals the in-repo model.

    The client already pins ``authorization_model_id`` on every call, so
    ``read_authorization_model`` returns exactly that model; any structural drift
    (added relation, rewritten ``viewer``, changed schema) raises
    :class:`ModelPinError`.
    """
    if not settings.authorization_model_id:
        raise ModelPinError("authorization_model_id is empty while ReBAC is enabled")
    server = dict(await client.read_authorization_model())
    if _comparable(server) != _comparable(load_model()):
        raise ModelPinError("server authorization model does not match the pinned in-repo model")


# --- operator bootstrap CLI (`python -m ne.permissions_rebac.bootstrap`) ----


def _default_client_factory(api_url: str, api_token: str) -> _BootstrapSurface:
    # Function-level import keeps openfga_sdk out of the eager graph (C3).
    from ne.permissions_rebac._client import BootstrapClient

    return BootstrapClient(api_url, api_token)


def _toml_snippet(api_url: str, store_id: str, model_id: str) -> str:
    return (
        "# config/ne-rebac.toml — paste into the pilot bundle, then re-promote.\n"
        "enabled = true\n"
        f'api_url = "{api_url}"\n'
        f'store_id = "{store_id}"\n'
        f'authorization_model_id = "{model_id}"\n'
    )


async def _run_bootstrap(
    api_url: str,
    store_name: str,
    api_token: str,
    client_factory: Callable[[str, str], _BootstrapSurface],
) -> tuple[str, str]:
    client = client_factory(api_url, api_token)
    try:
        store_id = await client.create_store(store_name)
        model_id = await client.write_authorization_model(load_model())
    finally:
        await client.close()
    return store_id, model_id


def main(
    argv: list[str] | None = None,
    *,
    client_factory: Callable[[str, str], _BootstrapSurface] = _default_client_factory,
    out: Any | None = None,
) -> int:
    """Create the store, write the pinned model, print the ids + a TOML snippet.

    The preshared key is read ONLY from the environment (``NE_REBAC_API_TOKEN``),
    never a CLI argument (C12 — argv is world-readable in ``ps``). The URL is
    validated in-VPC (fail-closed) BEFORE any credential is used.
    """
    import sys

    stream = out if out is not None else sys.stdout
    parser = argparse.ArgumentParser(prog="ne.permissions_rebac.bootstrap")
    parser.add_argument("--api-url", required=True, help="in-VPC OpenFGA HTTP API URL (port 8080)")
    parser.add_argument("--store-name", default="neutron", help="store name to create")
    args = parser.parse_args(argv)

    # Validate the endpoint up front (fail-closed; never receives the token if bad).
    validate_openfga_url(args.api_url)
    api_token = os.environ.get("NE_REBAC_API_TOKEN", "")

    store_id, model_id = asyncio.run(
        _run_bootstrap(args.api_url, args.store_name, api_token, client_factory)
    )
    print(f"store_id={store_id}", file=stream)
    print(f"authorization_model_id={model_id}", file=stream)
    print("", file=stream)
    print(_toml_snippet(args.api_url, store_id, model_id), file=stream)
    return 0


if __name__ == "__main__":  # pragma: no cover - module CLI entrypoint
    raise SystemExit(main())
