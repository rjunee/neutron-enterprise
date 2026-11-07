# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""THE single OpenFGA SDK import chokepoint (C3 isolation).

This module is the ONLY place in the repository permitted to import
``openfga_sdk``; every other ``ne.permissions_rebac`` module talks to OpenFGA
through the plain-string :class:`~ne.permissions_rebac.ports.OpenFgaTupleClient`
Protocol. Modeled exactly on ``ne.gateway._litellm`` / ``ne.mcp._sdk``. Enforced
by the ``c3-openfga-sdk-chokepoint`` import-linter contract (sole ignored edge
``ne.permissions_rebac._client -> openfga_sdk``).

Every call carries the PINNED ``authorization_model_id`` (set on the
``ClientConfiguration``), so a drifted server-side model is unreachable by
construction. Backend / timeout / HTTP failures are mapped to
:class:`_BackendError` that the resolver and share service translate per mode
(degrade vs deny) — EXCEPT ``delete_tuples``' one narrow case: the server's
specific "tuple already absent" rejection is mapped to
:class:`~ne.permissions_rebac.errors.TupleAbsentError` (a ``BackendError``
subclass, NE-REBAC-REVOKE-IDEMPOTENT) so ``ShareService.revoke`` can treat
that ONE server-authoritative signal as its own success. No error surfaces
the ``api_url`` or the preshared key.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from openfga_sdk import (
    ClientConfiguration,
    CreateStoreRequest,
    OpenFgaClient,
    ReadRequestTupleKey,
    WriteAuthorizationModelRequest,
)
from openfga_sdk.client.models import ClientListObjectsRequest, ClientTuple
from openfga_sdk.credentials import CredentialConfiguration, Credentials
from openfga_sdk.exceptions import OpenApiException, ValidationException

from ne.permissions_rebac.config import RebacSettings
from ne.permissions_rebac.errors import BackendError as _BackendError
from ne.permissions_rebac.errors import ModelPinError, RebacDisabledError, TupleAbsentError
from ne.permissions_rebac.ports import Tuple3
from ne.permissions_rebac.sovereignty import validate_openfga_url

__all__ = ["BootstrapClient", "OpenFgaHttpClient"]

#: The real OpenFGA server's exact wording (observed 2026-07-24 against
#: openfga/openfga:v1.18.1) for a 400 ``write_failed_due_to_invalid_input``
#: rejecting a transactional delete of a tuple that does not exist. OpenFGA
#: reuses the SAME error code for duplicate-write and absent-delete, so this
#: message substring is still needed to discriminate between those two
#: (NE-REBAC-REVOKE-IDEMPOTENT). If a future server upgrade rewords this,
#: ``_is_absent_delete`` stops matching and ``delete_tuples`` degrades to a
#: plain ``BackendError`` — the pre-fix, fail-safe behavior (loud error,
#: never silent false success); the pinned live test
#: (``test_live_delete_semantics_pinned``) turns red.
_ABSENT_DELETE_MARKER = "cannot delete a tuple which does not exist"

#: The server's error ``code`` (observed 2026-07-24, openfga/openfga:v1.18.1)
#: for the write-transaction-validation family that BOTH the absent-delete
#: rejection and duplicate-write rejection share. Required (status == 400 AND
#: code == this) as a structural pre-check ALONGSIDE the message substring
#: above — not instead of it. Without this check, a message-substring-only
#: match would misclassify an unrelated ``validation_error`` (e.g. a
#: malformed tuple whose id happens to embed the marker text, which OpenFGA
#: echoes verbatim into ``error_message`` — see
#: ``test_live_non_absent_delete_error_not_swallowed``) as an absent-delete,
#: causing ``ShareService.revoke`` to suppress a real failure and report a
#: false success (Codex C1).
_ABSENT_DELETE_CODE = "write_failed_due_to_invalid_input"


def _is_absent_delete(exc: OpenApiException) -> bool:
    """Whether ``exc`` is the server's specific absent-delete rejection.

    Total by construction — NEVER raises, defaults to ``False`` on anything
    unexpected (a different exception shape, an unset/undecodable body). Only
    a :class:`ValidationException` with ``status == 400``, ``code ==
    "write_failed_due_to_invalid_input"``, AND a message that names the tuple
    as absent (case-insensitively) counts; every other ``OpenApiException``
    (network fault, auth failure, timeout, a differently-coded 400 such as a
    malformed tuple) returns ``False`` and propagates as ``BackendError``
    untouched. The status+code pre-check is structural and must pass BEFORE
    the message substring is even consulted, so a malformed-tuple id that
    happens to contain the marker text cannot be misclassified.
    """
    if not isinstance(exc, ValidationException):
        return False
    if getattr(exc, "status", None) != 400:
        return False
    if getattr(exc, "code", None) != _ABSENT_DELETE_CODE:
        return False
    message = getattr(exc, "error_message", None)
    if not message:
        body: Any = getattr(exc, "body", None)
        if isinstance(body, bytes):
            message = body.decode("utf-8", errors="replace")
        elif isinstance(body, str):
            message = body
        else:
            message = getattr(exc, "reason", None) or ""
    return _ABSENT_DELETE_MARKER.lower() in str(message).lower()


def _client_config(settings: RebacSettings) -> ClientConfiguration:
    """Build a pinned, credentialed, in-VPC-validated ClientConfiguration."""
    api_url = validate_openfga_url(settings.api_url)
    credentials: Credentials | None = None
    token = settings.api_token.get_secret_value()
    if token:
        credentials = Credentials(
            method="api_token",
            configuration=CredentialConfiguration(api_token=token),
        )
    return ClientConfiguration(
        api_url=api_url,
        store_id=settings.store_id,
        authorization_model_id=settings.authorization_model_id,
        credentials=credentials,
        timeout_millisec=int(settings.timeout_s * 1000),
    )


class OpenFgaHttpClient:
    """:class:`OpenFgaTupleClient` over the async ``openfga_sdk.OpenFgaClient``.

    Raises :class:`RebacDisabledError` if constructed while the module is
    disabled, and :class:`ModelPinError` if the authorization-model pin is empty
    (a pinned model id is mandatory — every call carries it).
    """

    def __init__(self, settings: RebacSettings) -> None:
        if not settings.enabled:
            raise RebacDisabledError("OpenFgaHttpClient constructed while ReBAC is disabled")
        if not settings.authorization_model_id:
            raise ModelPinError("authorization_model_id must be pinned (non-empty) when enabled")
        # Build the (loop-free) config eagerly — validate_openfga_url runs NOW so a
        # bad endpoint fails at construction (fail-closed). The underlying
        # ``OpenFgaClient`` (which opens an aiohttp session bound to the running
        # event loop) is created LAZILY on first call: the composition root
        # constructs this synchronously OUTSIDE any loop, and the session must bind
        # to the SERVING loop — never the factory's. No server contact at __init__.
        self._config = _client_config(settings)
        self._lazy_client: OpenFgaClient | None = None

    def _get(self) -> OpenFgaClient:
        if self._lazy_client is None:
            self._lazy_client = OpenFgaClient(self._config)
        return self._lazy_client

    async def close(self) -> None:
        if self._lazy_client is not None:
            await self._lazy_client.close()
            self._lazy_client = None

    @staticmethod
    def _tuples(triples: Sequence[Tuple3]) -> list[ClientTuple]:
        return [ClientTuple(user=u, relation=r, object=o) for (u, r, o) in triples]

    async def list_objects(
        self,
        user: str,
        relation: str,
        object_type: str,
        *,
        contextual_tuples: Sequence[Tuple3],
    ) -> Sequence[str]:
        body = ClientListObjectsRequest(
            user=user,
            relation=relation,
            type=object_type,
            contextual_tuples=self._tuples(contextual_tuples),
        )
        try:
            response = await self._get().list_objects(body)
        except OpenApiException as exc:
            raise _BackendError("openfga list_objects failed") from exc
        objects: Any = getattr(response, "objects", None) or []
        return [str(o) for o in objects]

    async def write_tuples(self, tuples: Sequence[Tuple3]) -> None:
        try:
            await self._get().write_tuples(self._tuples(tuples))
        except OpenApiException as exc:
            raise _BackendError("openfga write_tuples failed") from exc

    async def delete_tuples(self, tuples: Sequence[Tuple3]) -> None:
        try:
            await self._get().delete_tuples(self._tuples(tuples))
        except OpenApiException as exc:
            if _is_absent_delete(exc):
                raise TupleAbsentError("openfga delete_tuples: tuple already absent") from exc
            raise _BackendError("openfga delete_tuples failed") from exc

    async def read_tuples(
        self, *, object_filter: str | None = None, user_filter: str | None = None
    ) -> Sequence[Tuple3]:
        key = ReadRequestTupleKey(user=user_filter, object=object_filter)
        out: list[Tuple3] = []
        continuation: str | None = None
        try:
            while True:
                options: dict[str, Any] = {}
                if continuation:
                    options["continuation_token"] = continuation
                response = await self._get().read(key, options or None)
                for tup in getattr(response, "tuples", None) or []:
                    tk: Any = getattr(tup, "key", None)
                    if tk is not None:
                        out.append((str(tk.user), str(tk.relation), str(tk.object)))
                continuation = getattr(response, "continuation_token", None) or None
                if not continuation:
                    break
        except OpenApiException as exc:
            raise _BackendError("openfga read failed") from exc
        return out

    async def read_authorization_model(self) -> Mapping[str, Any]:
        try:
            response = await self._get().read_authorization_model()
        except OpenApiException as exc:
            raise _BackendError("openfga read_authorization_model failed") from exc
        model: Any = getattr(response, "authorization_model", None)
        if model is None:
            return {}
        serialized: Any = model.to_dict(serialize=True)
        return dict(serialized) if isinstance(serialized, Mapping) else {}

    # -- bootstrap only ------------------------------------------------------
    async def create_store(self, name: str) -> str:
        try:
            response = await self._get().create_store(CreateStoreRequest(name=name))
        except OpenApiException as exc:
            raise _BackendError("openfga create_store failed") from exc
        return str(getattr(response, "id", ""))

    async def write_authorization_model(self, model: Mapping[str, Any]) -> str:
        request = WriteAuthorizationModelRequest(
            schema_version=model.get("schema_version"),
            type_definitions=model.get("type_definitions"),
            conditions=model.get("conditions"),
        )
        try:
            response = await self._get().write_authorization_model(request)
        except OpenApiException as exc:
            raise _BackendError("openfga write_authorization_model failed") from exc
        return str(getattr(response, "authorization_model_id", ""))


def _bootstrap_config(api_url: str, api_token: str, timeout_s: float) -> ClientConfiguration:
    """A store/model-UNPINNED, in-VPC-validated ClientConfiguration for bootstrap."""
    validated = validate_openfga_url(api_url)
    credentials: Credentials | None = None
    if api_token:
        credentials = Credentials(
            method="api_token",
            configuration=CredentialConfiguration(api_token=api_token),
        )
    return ClientConfiguration(
        api_url=validated,
        credentials=credentials,
        timeout_millisec=int(timeout_s * 1000),
    )


class BootstrapClient:
    """The DELIBERATELY narrow un-pinned surface used ONLY by the operator CLI.

    A store must be created and a model written BEFORE any ``authorization_model_id``
    exists, so the pinned :class:`OpenFgaHttpClient` (which requires the pin) cannot
    perform bootstrap. This class exposes ONLY ``create_store`` /
    ``write_authorization_model`` / ``close`` — a distinct type so the un-pinned
    surface can never be wired into the resolver (which takes an
    :class:`~ne.permissions_rebac.ports.OpenFgaTupleClient`; this class is NOT one).
    It has no ``enabled`` gate: bootstrap runs precisely when the module is not yet
    enabled.
    """

    def __init__(self, api_url: str, api_token: str = "", *, timeout_s: float = 5.0) -> None:
        self._client = OpenFgaClient(_bootstrap_config(api_url, api_token, timeout_s))

    async def close(self) -> None:
        await self._client.close()

    async def create_store(self, name: str) -> str:
        try:
            response = await self._client.create_store(CreateStoreRequest(name=name))
        except OpenApiException as exc:
            raise _BackendError("openfga create_store failed") from exc
        store_id = str(getattr(response, "id", ""))
        # Bind subsequent model writes to the freshly-created store.
        self._client.set_store_id(store_id)
        return store_id

    async def write_authorization_model(self, model: Mapping[str, Any]) -> str:
        request = WriteAuthorizationModelRequest(
            schema_version=model.get("schema_version"),
            type_definitions=model.get("type_definitions"),
            conditions=model.get("conditions"),
        )
        try:
            response = await self._client.write_authorization_model(request)
        except OpenApiException as exc:
            raise _BackendError("openfga write_authorization_model failed") from exc
        return str(getattr(response, "authorization_model_id", ""))
