# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``build_scim_app`` — the mounted ``/scim/v2`` ASGI app (SPEC row NE-089).

A SEPARATE small FastAPI app (NOT the OIDC-wrapped inner app; see
``ne.api.app`` step 12's ordering comment) carrying the decided SCIM subset:
Users + Groups CRUD/PATCH, the two supported filter shapes, and the static
discovery documents. Every response — success or error — is served as
``application/scim+json``. Malformed PATCH bodies are parsed manually (not via
FastAPI's automatic pydantic body binding) so a syntax error maps to the SCIM
400 ``invalidSyntax`` shape rather than FastAPI's default 422.

C12 — a refusal names the FIELD, never the VALUE. Three mechanisms, and the
sweep in ``tests/test_scim_c12_refusals.py`` is what pins each, not this
sentence:

* every refusal ``detail`` raised in this package is a string LITERAL (or the
  verbatim ``detail`` of one, or the field-only composer) — pinned by the
  package-wide AST walk ``test_every_scim_refusal_site_is_literal_or_explicitly_justified``,
  which discovers raise sites rather than listing them;
* a refusal composed from a pydantic failure goes through
  ``describe_validation_error``, because ``str(exc)`` on a
  ``ValidationError``/``RequestValidationError`` renders the rejected value and
  a SCIM body is user identity attributes;
* a refusal raised inside an ``except`` severs the chain with ``from None``,
  since a chained cause resurfaces that value as ``input_value=`` in
  ``traceback.format_exception`` output.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

from ne.api.scim.auth import build_scim_bearer_dependency
from ne.api.scim.filters import ScimFilterError, parse_eq_filter
from ne.api.scim.models import (
    ListResponse,
    PatchRequest,
    ScimError,
    ScimGroup,
    ScimUser,
    describe_validation_error,
)
from ne.api.scim.service import (
    NO_SUCH_GROUP,
    NO_SUCH_USER,
    ScimBadRequestError,
    ScimNotFoundError,
    ScimProvisioningService,
    ScimServiceError,
)

__all__ = ["build_scim_app"]

_MEDIA_TYPE = "application/scim+json"

_USER_FILTER_ATTRS = {"username": "userName", "externalid": "externalId"}
_GROUP_FILTER_ATTRS = {"displayname": "displayName", "externalid": "externalId"}

_SERVICE_PROVIDER_CONFIG: dict[str, Any] = {
    "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
    "patch": {"supported": True},
    "filter": {"supported": True, "maxResults": 200},
    "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
    "sort": {"supported": False},
    "etag": {"supported": False},
    "changePassword": {"supported": False},
    "authenticationSchemes": [
        {
            "type": "oauthbearertoken",
            "name": "OAuth Bearer Token",
            "description": "A shared bearer secret issued out-of-band (NE_API_SCIM_BEARER_TOKEN).",
        }
    ],
}

_RESOURCE_TYPES: list[dict[str, Any]] = [
    {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
        "id": "User",
        "name": "User",
        "endpoint": "/Users",
        "schema": "urn:ietf:params:scim:schemas:core:2.0:User",
    },
    {
        "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
        "id": "Group",
        "name": "Group",
        "endpoint": "/Groups",
        "schema": "urn:ietf:params:scim:schemas:core:2.0:Group",
    },
]

_SCHEMAS: list[dict[str, Any]] = [
    {
        "id": "urn:ietf:params:scim:schemas:core:2.0:User",
        "name": "User",
        "description": "NE-089 supported subset: userName, externalId, displayName, active.",
    },
    {
        "id": "urn:ietf:params:scim:schemas:core:2.0:Group",
        "name": "Group",
        "description": "NE-089 supported subset: displayName, externalId, members.",
    },
]


def _scim_response(payload: Any, status_code: int = 200) -> Response:
    if hasattr(payload, "model_dump_json"):
        body = payload.model_dump_json(exclude_none=True)
    else:
        body = json.dumps(payload)
    return Response(content=body, status_code=status_code, media_type=_MEDIA_TYPE)


def _error_response(status_code: int, scim_type: str | None, detail: str) -> Response:
    err = ScimError(status=str(status_code), scimType=scim_type, detail=detail)
    return _scim_response(err, status_code)


async def _parse_patch_request(request: Request) -> PatchRequest:
    try:
        raw = await request.json()
    except Exception as exc:
        # ``from None`` — DEFENSE IN DEPTH at this link specifically, and no wider
        # claim than that. The causes reachable today (``json.JSONDecodeError``,
        # ``UnicodeDecodeError``) render a POSITION and never body content, but
        # this ``except`` clause is unbounded, so nothing here pins the cause
        # class. The position is the whole diagnostic value of the cause, so it is
        # carried forward explicitly instead — a line/column offset, never text.
        where = (
            f" at line {exc.lineno} column {exc.colno}"
            if isinstance(exc, json.JSONDecodeError)
            else ""
        )
        raise ScimBadRequestError("invalidSyntax", f"malformed JSON body{where}") from None
    try:
        return PatchRequest.model_validate(raw)
    except ValidationError as exc:
        # C12, both halves. The MESSAGE is composed field-only by
        # ``describe_validation_error`` (``str(exc)`` renders the rejected value),
        # and ``from None`` severs the chain, whose rendering puts that same value
        # back as ``input_value=`` in ``traceback.format_exception`` output. A
        # SCIM PATCH body carries userName/displayName/member ids.
        raise ScimBadRequestError(
            "invalidSyntax",
            describe_validation_error(
                exc.errors(include_url=False, include_context=False, include_input=False)
            ),
        ) from None


def build_scim_app(service: ScimProvisioningService, token: str) -> FastAPI:
    """Construct the ``/scim/v2`` app — bearer-gated at router level."""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    require_bearer = build_scim_bearer_dependency(token)
    router = APIRouter(dependencies=[Depends(require_bearer)])

    # -- discovery ------------------------------------------------------

    @router.get("/ServiceProviderConfig")
    async def _service_provider_config() -> Response:
        return _scim_response(_SERVICE_PROVIDER_CONFIG)

    @router.get("/ResourceTypes")
    async def _resource_types() -> Response:
        return _scim_response(_RESOURCE_TYPES)

    @router.get("/Schemas")
    async def _schemas() -> Response:
        return _scim_response(_SCHEMAS)

    # -- Users ------------------------------------------------------------

    @router.post("/Users", status_code=201)
    async def _create_user(body: ScimUser) -> Response:
        user = await service.create_user(body)
        return _scim_response(user, 201)

    @router.get("/Users")
    async def _list_users(
        filter: str | None = Query(default=None),
        startIndex: int = Query(default=1, ge=1),
        count: int = Query(default=100, ge=0),
    ) -> Response:
        filter_attr: str | None = None
        filter_value: str | None = None
        if filter is not None:
            try:
                filter_attr, filter_value = parse_eq_filter(filter, _USER_FILTER_ATTRS)
            except ScimFilterError as exc:
                # ``from None``: see ``_parse_patch_request``. Defense in depth at
                # THIS link and no wider claim — the cause is our own
                # ``ScimFilterError``, whose message is copied verbatim into the
                # error raised here, so the chain can add nothing this raise does
                # not already say. Severing means a future value echoed back into
                # ``ScimFilterError`` cannot ride this link into a formatted chain.
                raise ScimBadRequestError(exc.scim_type, exc.detail) from None
        users, total = service.list_users(
            filter_attr=filter_attr, filter_value=filter_value, start_index=startIndex, count=count
        )
        resp = ListResponse(
            totalResults=total,
            startIndex=startIndex,
            itemsPerPage=len(users),
            Resources=tuple(u.model_dump(mode="json", exclude_none=True) for u in users),
        )
        return _scim_response(resp)

    @router.get("/Users/{user_id}")
    async def _get_user(user_id: str) -> Response:
        user = service.get_user(user_id)
        if user is None:
            raise ScimNotFoundError(NO_SUCH_USER)
        return _scim_response(user)

    @router.put("/Users/{user_id}")
    async def _put_user(user_id: str, body: ScimUser) -> Response:
        user = await service.put_user(user_id, body)
        return _scim_response(user)

    @router.patch("/Users/{user_id}")
    async def _patch_user(user_id: str, request: Request) -> Response:
        patch_req = await _parse_patch_request(request)
        user = await service.patch_user(user_id, patch_req.Operations)
        return _scim_response(user)

    @router.delete("/Users/{user_id}", status_code=204)
    async def _delete_user(user_id: str) -> Response:
        await service.delete_user(user_id)
        return Response(status_code=204)

    # -- Groups -------------------------------------------------------------

    @router.post("/Groups", status_code=201)
    async def _create_group(body: ScimGroup) -> Response:
        group = await service.create_group(body)
        return _scim_response(group, 201)

    @router.get("/Groups")
    async def _list_groups(
        filter: str | None = Query(default=None),
        startIndex: int = Query(default=1, ge=1),
        count: int = Query(default=100, ge=0),
    ) -> Response:
        filter_attr: str | None = None
        filter_value: str | None = None
        if filter is not None:
            try:
                filter_attr, filter_value = parse_eq_filter(filter, _GROUP_FILTER_ATTRS)
            except ScimFilterError as exc:
                raise ScimBadRequestError(exc.scim_type, exc.detail) from None  # see _list_users
        groups, total = service.list_groups(
            filter_attr=filter_attr, filter_value=filter_value, start_index=startIndex, count=count
        )
        resp = ListResponse(
            totalResults=total,
            startIndex=startIndex,
            itemsPerPage=len(groups),
            Resources=tuple(g.model_dump(mode="json", exclude_none=True) for g in groups),
        )
        return _scim_response(resp)

    @router.get("/Groups/{group_id}")
    async def _get_group(group_id: str) -> Response:
        group = service.get_group(group_id)
        if group is None:
            raise ScimNotFoundError(NO_SUCH_GROUP)
        return _scim_response(group)

    @router.put("/Groups/{group_id}")
    async def _put_group(group_id: str, body: ScimGroup) -> Response:
        group = await service.put_group(group_id, body)
        return _scim_response(group)

    @router.patch("/Groups/{group_id}")
    async def _patch_group(group_id: str, request: Request) -> Response:
        patch_req = await _parse_patch_request(request)
        group = await service.patch_group(group_id, patch_req.Operations)
        return _scim_response(group)

    @router.delete("/Groups/{group_id}", status_code=204)
    async def _delete_group(group_id: str) -> Response:
        await service.delete_group(group_id)
        return Response(status_code=204)

    app.include_router(router)

    async def _on_scim_error(_request: Request, exc: Exception) -> Response:
        assert isinstance(exc, ScimServiceError)
        return _error_response(exc.status, exc.scim_type, exc.detail)

    async def _on_http_exception(_request: Request, exc: Exception) -> Response:
        # CONDUIT. Every ``StarletteHTTPException`` reaching this mount is raised
        # either by ``ne.api.scim.auth`` (the constant ``"unauthorized"``, never the
        # supplied token) or by Starlette's own routing (``"Not Found"`` /
        # ``"Method Not Allowed"``) — fixed vocabulary on both sides, no caller text.
        assert isinstance(exc, StarletteHTTPException)
        detail = exc.detail if isinstance(exc.detail, str) else "error"
        resp = _error_response(exc.status_code, None, detail)
        if exc.headers:
            for key, value in exc.headers.items():
                resp.headers[key] = value
        return resp

    async def _on_validation_error(_request: Request, exc: Exception) -> Response:
        # C12: ``str(exc)`` on a ``RequestValidationError`` renders each error's
        # ``input`` — for a missing top-level field that is the WHOLE request body,
        # so a single mistyped ``active`` on ``POST /Users`` would return every
        # identity attribute the IdP just pushed. This is the FastAPI-bound half of
        # the surface (create/replace bodies, protocol query params); the
        # hand-parsed PATCH envelope composes through the same function.
        #
        # RESIDUAL, named rather than claimed away: unlike the ``from None`` raise
        # sites this handler has nothing to sever — Starlette builds the
        # ``RequestValidationError`` OBJECT before we see it and its formatted chain
        # still carries the whole body. The wire body composed below is clean, and
        # the object is consumed by Starlette's ``ExceptionMiddleware`` rather than
        # propagating out of the app, so nothing in THIS composition renders it.
        # It goes live the day ASGI instrumentation that records HANDLED exceptions
        # is added.
        assert isinstance(exc, RequestValidationError)
        return _error_response(400, "invalidSyntax", describe_validation_error(exc.errors()))

    app.add_exception_handler(ScimServiceError, _on_scim_error)
    app.add_exception_handler(StarletteHTTPException, _on_http_exception)
    app.add_exception_handler(RequestValidationError, _on_validation_error)

    return app
