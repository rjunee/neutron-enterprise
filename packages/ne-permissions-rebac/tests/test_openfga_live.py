# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""LIVE replay of the no-bypass suite against a REAL OpenFGA server (NE-LP-REBAC).

Everything in ``test_resolver.py`` / ``test_share_service.py`` /
``test_fakes_conformance.py`` runs against ``InMemoryOpenFga`` — a hand-written
fake. This module is an ADDITIONAL layer on top of that fast unit-test surface
(``ne.permissions_rebac.testing.fakes`` stays the CI default; this file never
weakens or replaces it): it bootstraps a REAL authorization model via a REAL
``WriteAuthorizationModel`` call and replays the resolver / ``ShareService``
no-bypass flow through the REAL ``OpenFgaHttpClient`` / ``BootstrapClient``
(the ``ne.permissions_rebac._client`` SDK chokepoint), including at least one
genuine read-after-write and one read-after-delete ``ListObjects`` consistency
assertion with NO sleeps and NO retries — a lag there IS the finding this lane
exists to catch (SPEC §5 NE-LP-REBAC).

Run locally against a real server:

    docker compose -p ne-rebac-liveproof -f deploy/compose/dev/compose.yaml \\
      --profile rebac up -d --wait --wait-timeout 300 openfga

    NE_TEST_OPENFGA_URL=http://127.0.0.1:8087 \\
      NE_TEST_OPENFGA_TOKEN=nedev-openfga-token \\
      uv run pytest packages/ne-permissions-rebac/tests/test_openfga_live.py -q -rs

    docker compose -p ne-rebac-liveproof -f deploy/compose/dev/compose.yaml \\
      --profile rebac down -v --remove-orphans

Env vars (both default to the dev-compose ``rebac`` profile's well-known
values, so a bare `uv run pytest` against a locally-running rig needs none of
this set):

* ``NE_TEST_OPENFGA_URL`` — OpenFGA HTTP API base URL (default
  ``http://127.0.0.1:8087``).
* ``NE_TEST_OPENFGA_TOKEN`` — preshared API token (default
  ``nedev-openfga-token``, the committed dev-only well-known cred — never a
  real credential; see C12 in the package README).

Three fake-vs-real divergences are pinned here as OBSERVED reality (never by
editing ``testing/fakes.py`` or ``src/``) — see
``packages/ne-permissions-rebac/README.md`` "Known fake-vs-real divergences"
for the full ledger:

* duplicate tuple write: the fake raises ``ValueError``; the real server
  raises the confined-boundary ``BackendError``.
* delete of a never-written tuple — **finding NE-LP-REBAC-D1, RESOLVED by
  NE-REBAC-REVOKE-IDEMPOTENT (2026-07-24)**: this was first observed here as
  a divergence — the fake discarded a missing tuple idempotently (per
  ``ShareService.revoke``'s then-"idempotent" comment) while the real server
  400s a transactional delete of a missing tuple. The contract was decided
  deliberately (genuinely idempotent) and both sides now AGREE:
  ``_client.py`` maps EXACTLY that server signature to
  :class:`~ne.permissions_rebac.errors.TupleAbsentError` (a narrow
  ``BackendError`` subclass — every other delete failure still raises plain
  ``BackendError``), ``ShareService.revoke`` catches only that type, and
  ``InMemoryOpenFga`` was corrected to raise the same typed error
  transactionally so the fake can no longer be more forgiving than
  production. See ``test_live_delete_semantics_pinned`` (the precise type),
  ``test_live_revoke_idempotent_end_to_end`` (the resulting contract, live),
  and ``test_live_non_absent_delete_error_not_swallowed`` (the narrow-catch
  negative, live).
* stored (not contextual) group-membership tuples: the fake models
  ``group#member`` as resolvable ONLY via CONTEXTUAL tuples (mirroring the
  resolver's own per-query-derivation design); the real server, given
  ``neutron.fga``'s unrestricted ``member: [user]`` relation, resolves a
  STORED membership tuple too. This is real OpenFGA model semantics, not a
  bug — the "membership is never stored" guarantee is enforced at the
  WRITER layer (``ShareService`` never accepts a ``member`` relation or a
  caller-supplied ``#`` subject), which this module proves holds
  independently of what the server itself would allow if written directly.

**FORMERLY-UNANTICIPATED FINDING NE-LP-REBAC-D2** (discovered while building
this lane; **fixed by NE-REBAC-MODELPIN**, 2026-07-24): ``bootstrap.
verify_model_pin`` — the fail-closed startup drift-detection gate — used to
be non-functional against a real OpenFGA server: it raised ``ModelPinError``
unconditionally, even when there was genuinely ZERO drift from the in-repo
pinned model. Root cause: the OpenFGA Python SDK's generated
``to_dict(serialize=True)`` unconditionally emits every declared OpenAPI
field for every nested object — nulls and empty-string defaults included
(``"condition": ""``, ``"wildcard": null``, ``"module": ""``,
``"source_info": null``, …) — while the in-repo ``neutron.model.json`` is
written sparse; ``bootstrap._canonical()`` sorted but never stripped those
SDK-injected defaults, so the two dicts could never structurally match.
``InMemoryOpenFga`` never caught this because its fake
``read_authorization_model`` just echoes back exactly what was stored — this
is precisely the class of divergence a live server was needed to surface.
It never caused a wrong allow/deny authz decision (every real
``list_objects``/``write_tuples``/etc. call is independently scoped by the
pinned ``authorization_model_id`` set on the SDK's ``ClientConfiguration`` —
enforcement was unaffected), so it failed closed/unusable rather than open —
but it meant the startup pin-drift gate could never pass against a real
server, at all. **Fix**: ``bootstrap._without_unset_defaults`` now strips
SDK-injected raw None/""/[] defaults symmetrically on both sides inside
``_comparable`` before canonicalizing. Raw ``{}`` is stripped too, EXCEPT at
the two OpenFGA presence-marker field names (``bootstrap._PRESENCE_MARKER_
KEYS``: ``"this"``, ``"wildcard"``), where an empty payload is itself the
semantic signal, not an unset default (``Userset.this`` = direct rewrite,
``RelationReference.wildcard`` = ``user:*``/PUBLIC) — those two must keep
comparing unequal to their absence for the check to stay non-vacuous. Two
real-server divergences from the local SDK round-trip were found and fixed
during this build, each caught by a DIFFERENT proof layer: (1) an earlier
version of this fix blanket-dropped every raw ``{}``, non-vacuous only by
accident for ``this`` (protected by an added dict key) but not for
``wildcard`` (a sibling field) — caught by review, before merge; (2) the
REAL live server round-trips an empty ``relations`` map or the top-level
``conditions`` map as raw ``{}`` (not ``null``, unlike the local
``ApiClient.__deserialize`` round-trip the fake-lane tests use), which a
naive "never strip ``{}``" fix would NOT strip and would therefore raise on
an undrifted model — caught by THIS module's mandatory live run, before
merge. The tests in this module now prove three drift classes still raise
against the real server, alongside the undrifted-passes proof
(``test_live_bootstrap_and_model_pin_verifies``): ADD-class
(``test_live_model_pin_isolates_from_drifted_latest``, an added relation),
ALTER-class (``test_live_model_pin_rejects_rewritten_relation``, a rewritten
union), and PRESENCE-class (``test_live_model_pin_rejects_wildcard_widening``,
a sibling-field ``wildcard: {}`` widening to ``user:*``).
"""

from __future__ import annotations

import asyncio
import copy
import os
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import pytest
from pydantic import SecretStr

from ne.contracts.governance import DecisionEffect
from ne.contracts.types import Principal
from ne.permissions_rebac._client import BootstrapClient, OpenFgaHttpClient
from ne.permissions_rebac.bootstrap import load_model, verify_model_pin
from ne.permissions_rebac.config import RebacSettings
from ne.permissions_rebac.errors import (
    BackendError,
    ModelPinError,
    RebacAuditError,
    ShareNotPermittedError,
    ShareValidationError,
    TupleAbsentError,
)
from ne.permissions_rebac.resolver import OpenFgaSharedDocumentResolver
from ne.permissions_rebac.share import ShareService
from ne.permissions_rebac.testing.fakes import (
    FailingAuditSink,
    FakeGroupExpansion,
    FakeKnownPrincipals,
    FakeReadProbe,
    RecordingAuditSink,
    StaticPdp,
)

_URL = os.environ.get("NE_TEST_OPENFGA_URL", "http://127.0.0.1:8087")
_TOKEN = os.environ.get("NE_TEST_OPENFGA_TOKEN", "nedev-openfga-token")

# Copied verbatim from test_share_service.py (SHARER identity conventions).
SHARER = Principal(id="u-sharer")


def _openfga_available() -> bool:
    try:
        urllib.request.urlopen(f"{_URL}/healthz", timeout=1.0)
        return True
    except urllib.error.HTTPError:
        # A non-2xx (e.g. auth-shaped 401 on a misconfigured token) still means
        # a live server answered — reachable, just maybe not healthy/authed.
        return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


# MUST be a skipif pytestmark (never a module-level pytest.skip): the PR-CI
# static guardian (test_rebac_live_workflow_static.py) derives the workflow's
# must-run floor from `pytest --collect-only`, which requires collection to
# succeed even with the server down.
pytestmark = pytest.mark.skipif(
    not _openfga_available(),
    reason=(
        "OpenFGA test server not reachable "
        "(docker compose -p ne-rebac-liveproof -f deploy/compose/dev/compose.yaml "
        "--profile rebac up -d --wait --wait-timeout 300 openfga; "
        "see packages/ne-permissions-rebac/README.md)"
    ),
)


@asynccontextmanager
async def _live_client(
    model: Mapping[str, Any] | None = None,
) -> AsyncIterator[tuple[OpenFgaHttpClient, RebacSettings]]:
    """Bootstrap a fresh store + model against the REAL server, yield a live client.

    A fresh store per test is the isolation boundary (store creation is
    milliseconds on a local server); the ``OpenFgaHttpClient``'s aiohttp
    session binds to the running event loop, so this — like every caller —
    must be entered and exited inside the SAME ``asyncio.run`` as the rest of
    the test.
    """
    boot = BootstrapClient(_URL, _TOKEN)
    store_id = await boot.create_store(f"ne-live-{uuid4().hex[:12]}")
    model_id = await boot.write_authorization_model(model if model is not None else load_model())
    await boot.close()

    settings = RebacSettings(
        enabled=True,
        api_url=_URL,
        api_token=SecretStr(_TOKEN),
        store_id=store_id,
        authorization_model_id=model_id,
    )
    client = OpenFgaHttpClient(settings)
    try:
        yield client, settings
    finally:
        await client.close()


# --- bootstrap + model-pin isolation ----------------------------------------


def test_live_bootstrap_and_model_pin_verifies() -> None:
    """A real WriteAuthorizationModel round-trips faithfully — the pin gate PASSES.

    D2 fixed by NE-REBAC-MODELPIN: the model here was JUST written from
    ``load_model()`` with zero modification, so ``verify_model_pin`` — now
    that ``_comparable`` strips SDK-injected unset defaults symmetrically —
    raises nothing against the real server.
    """

    async def _body() -> None:
        async with _live_client() as (client, settings):
            await verify_model_pin(client, settings)  # no raise: undrifted, D2 fixed

    asyncio.run(_body())


def test_live_model_pin_isolates_from_drifted_latest() -> None:
    """A pinned client is unreachable-by-construction from a drifted server-latest.

    Two models are written into the SAME store: v1 (the real pinned model)
    and v2 (a drifted copy with an added ``editor`` relation, simulating a
    server-side edit that landed after this client was pinned). The core
    guarantee under test is that ``read_authorization_model()`` on a client
    pinned to v1 NEVER reflects v2's drift even though the store's LATEST
    model is now v2 (every real SDK call is scoped by the
    ``authorization_model_id`` fixed on its ``ClientConfiguration``) — proven
    directly on the returned model dict, independent of the D2 fix.
    ``verify_model_pin`` is also exercised on both pinned clients: with D2
    fixed (NE-REBAC-MODELPIN), it now correctly distinguishes "genuinely
    undrifted" (v1, passes) from "genuinely drifted, ADD-class" (v2, an
    added ``editor`` relation — still raises ``ModelPinError``).
    """

    async def _body() -> None:
        boot = BootstrapClient(_URL, _TOKEN)
        store_id = await boot.create_store(f"ne-live-{uuid4().hex[:12]}")
        model_v1 = load_model()
        model_id_v1 = await boot.write_authorization_model(model_v1)

        model_v2 = copy.deepcopy(model_v1)
        doc_type = next(t for t in model_v2["type_definitions"] if t["type"] == "document")
        doc_type["relations"]["editor"] = {"this": {}}
        doc_type["metadata"]["relations"]["editor"] = {
            "directly_related_user_types": [{"type": "user"}]
        }
        model_id_v2 = await boot.write_authorization_model(model_v2)
        await boot.close()

        settings_v1 = RebacSettings(
            enabled=True,
            api_url=_URL,
            api_token=SecretStr(_TOKEN),
            store_id=store_id,
            authorization_model_id=model_id_v1,
        )
        client_v1 = OpenFgaHttpClient(settings_v1)
        try:
            server_v1 = dict(await client_v1.read_authorization_model())
            doc_v1 = next(t for t in server_v1["type_definitions"] if t["type"] == "document")
            assert "editor" not in doc_v1.get("relations", {})
            await verify_model_pin(client_v1, settings_v1)  # undrifted -> passes (D2 fixed)
        finally:
            await client_v1.close()

        settings_v2 = RebacSettings(
            enabled=True,
            api_url=_URL,
            api_token=SecretStr(_TOKEN),
            store_id=store_id,
            authorization_model_id=model_id_v2,
        )
        client_v2 = OpenFgaHttpClient(settings_v2)
        try:
            server_v2 = dict(await client_v2.read_authorization_model())
            doc_v2 = next(t for t in server_v2["type_definitions"] if t["type"] == "document")
            assert "editor" in doc_v2.get("relations", {})
            with pytest.raises(ModelPinError):
                # genuinely drifted (added editor relation) -> still raises:
                # ADD-class non-vacuity proof
                await verify_model_pin(client_v2, settings_v2)
        finally:
            await client_v2.close()

    asyncio.run(_body())


def test_live_model_pin_rejects_rewritten_relation() -> None:
    """ALTER-class real-server mutation: a rewritten (not just added) relation still raises.

    Complements the ADD-class proof above: here ``document.viewer`` is
    rewritten from the owner-folding union down to a bare ``{"this": {}}``
    (owner no longer folds into viewer) — no relation added or removed, just
    altered — and ``verify_model_pin`` against the real server still raises
    ``ModelPinError``. Together with the ADD-class test, this proves the D2
    fix's default-stripping is not a blanket emptiness cascade that could
    mask a genuine rewrite.
    """

    async def _body() -> None:
        drifted = copy.deepcopy(load_model())
        doc = next(t for t in drifted["type_definitions"] if t["type"] == "document")
        doc["relations"]["viewer"] = {"this": {}}  # union dropped — owner no longer folds in
        async with _live_client(model=drifted) as (client, settings):
            with pytest.raises(ModelPinError):
                await verify_model_pin(client, settings)

    asyncio.run(_body())


# --- THE SPEC-row risk: real ListObjects consistency ------------------------


def test_live_model_pin_rejects_wildcard_widening() -> None:
    """PRESENCE-class real-server mutation: widening to `user:*` (wildcard) still raises.

    Complements the ADD-class and ALTER-class proofs above with the specific class
    flagged by review: ``RelationReference.wildcard`` is, like ``Userset.this``, a
    zero-field OpenFGA object whose PRESENCE (not content) is the signal — unlike
    ``this``, ``wildcard`` sits as a SIBLING key next to ``type``/``relation``/
    ``condition``, so a blanket "drop any raw {}" rule would silently accept a
    server-side model widened to PUBLIC (``user:*``) access: the single most
    privilege-escalating drift class the pin gate exists to catch (C6 fail-closed).
    Here ``document.viewer``'s first ``directly_related_user_types`` entry is
    widened from ``{"type": "user"}`` to ``{"type": "user", "wildcard": {}}`` on
    the REAL server and ``verify_model_pin`` still raises ``ModelPinError``.
    """

    async def _body() -> None:
        widened = copy.deepcopy(load_model())
        doc = next(t for t in widened["type_definitions"] if t["type"] == "document")
        refs = doc["metadata"]["relations"]["viewer"]["directly_related_user_types"]
        refs[0] = {**refs[0], "wildcard": {}}
        async with _live_client(model=widened) as (client, settings):
            with pytest.raises(ModelPinError):
                await verify_model_pin(client, settings)

    asyncio.run(_body())


def test_live_read_after_write_consistency_direct_grant() -> None:
    """Immediate ListObjects visibility after write, and after delete.

    NO sleeps, NO retries, NO eventual-consistency polling anywhere in this
    test — a lag here IS the finding NE-LP-REBAC exists to surface.
    """

    async def _body() -> None:
        async with _live_client() as (client, _settings):
            tup = ("user:jane", "viewer", "document:d1")

            await client.write_tuples([tup])
            objs = await client.list_objects(
                "user:jane", "viewer", "document", contextual_tuples=[]
            )
            assert objs == ["document:d1"]
            reads = await client.read_tuples(object_filter="document:d1")
            assert tup in reads

            await client.delete_tuples([tup])
            objs_after_delete = await client.list_objects(
                "user:jane", "viewer", "document", contextual_tuples=[]
            )
            assert objs_after_delete == []

    asyncio.run(_body())


def test_live_owner_folds_into_viewer() -> None:
    async def _body() -> None:
        async with _live_client() as (client, _settings):
            await client.write_tuples([("user:jane", "owner", "document:d1")])
            objs = await client.list_objects(
                "user:jane", "viewer", "document", contextual_tuples=[]
            )
            assert objs == ["document:d1"]

    asyncio.run(_body())


def test_live_group_member_resolves_only_via_contextual_tuples() -> None:
    async def _body() -> None:
        async with _live_client() as (client, _settings):
            await client.write_tuples([("group:g#member", "viewer", "document:d2")])

            objs_no_ctx = await client.list_objects(
                "user:jane", "viewer", "document", contextual_tuples=[]
            )
            assert objs_no_ctx == []

            objs_with_ctx = await client.list_objects(
                "user:jane",
                "viewer",
                "document",
                contextual_tuples=[("user:jane", "member", "group:g")],
            )
            assert objs_with_ctx == ["document:d2"]

    asyncio.run(_body())


def test_live_stored_membership_semantics_pinned() -> None:
    """OBSERVED: the real server resolves a STORED (not contextual) member tuple.

    See the module docstring's divergence note. This is real ``neutron.fga``
    ``member: [user]`` semantics, not a bug — and the writer-layer guarantee
    (``ShareService`` never writes/accepts a bare "member" relation or a
    caller-supplied ``#`` subject) is proven independently right here, so the
    stored-membership path can never be reached through the governed writer
    regardless of what the raw server would allow.
    """

    async def _body() -> None:
        async with _live_client() as (client, settings):
            await client.write_tuples(
                [
                    ("group:g1#member", "viewer", "document:stored-test"),
                    ("user:storeduser", "member", "group:g1"),  # STORED, not contextual
                ]
            )
            objs = await client.list_objects(
                "user:storeduser", "viewer", "document", contextual_tuples=[]
            )
            # OBSERVED (2026-07-24): resolves, opposite of
            # InMemoryOpenFga.test_stored_membership_tuple_grants_nothing.
            assert objs == ["document:stored-test"]

            known = FakeKnownPrincipals({"storeduser", "g1", SHARER.id})
            read_probe = FakeReadProbe({(SHARER.id, "stored-test")})
            svc = ShareService(
                client,
                known,
                read_probe,
                StaticPdp(DecisionEffect.ALLOW),
                RecordingAuditSink(),
                settings=settings,
            )
            with pytest.raises(ShareValidationError):
                await svc.share("stored-test", "user:storeduser", "member", sharer=SHARER)
            with pytest.raises(ShareValidationError):
                await svc.share("stored-test", "group:g1#member", sharer=SHARER)

    asyncio.run(_body())


def test_live_duplicate_write_raises_backend_error() -> None:
    """OBSERVED DIVERGENCE: the fake raises ValueError; the real server raises

    the confined-boundary BackendError (an OpenApiException underneath — the
    real server refuses to write a tuple that already exists). See
    README.md "Known fake-vs-real divergences".
    """

    async def _body() -> None:
        async with _live_client() as (client, _settings):
            tup = ("user:jane", "viewer", "document:d1")
            await client.write_tuples([tup])
            with pytest.raises(BackendError):
                await client.write_tuples([tup])

    asyncio.run(_body())


def test_live_delete_semantics_pinned() -> None:
    """RESOLVED finding NE-LP-REBAC-D1 (see module docstring) — pinned type.

    The REAL server 400s a transactional delete of a tuple that was never
    written. OBSERVED verbatim (2026-07-24, openfga/openfga:v1.18.1) via
    ``openfga_sdk.exceptions.ValidationException``: ``status=400``,
    ``code="write_failed_due_to_invalid_input"``,
    ``error_message="cannot delete a tuple which does not exist: user:
    'user:nobody', relation: 'viewer', object: 'document:ghost': tuple to be
    written already existed or the tuple to be deleted did not exist"``.
    ``_client.py`` maps EXACTLY this signature to the precise
    ``TupleAbsentError`` type (not just any ``BackendError``) so
    ``ShareService.revoke`` can catch it narrowly and deliver a genuinely
    idempotent revoke — see ``test_live_revoke_idempotent_end_to_end``.
    """

    async def _body() -> None:
        async with _live_client() as (client, _settings):
            with pytest.raises(TupleAbsentError):
                await client.delete_tuples([("user:nobody", "viewer", "document:ghost")])

    asyncio.run(_body())


def test_live_revoke_idempotent_end_to_end() -> None:
    """REAL server: share -> revoke -> revoke again -> revoke never-granted.

    All four calls succeed; the resolver shows the empty set for bob after
    the first revoke; the owner tuple survives (revoke targets the subject
    only); and every revoke call — including the no-op ones — appends its own
    ``rebac.share.revoked`` audit event (I-5: audit-then-mutate records
    revocation INTENT, not just a state change).
    """

    async def _body() -> None:
        async with _live_client() as (client, settings):
            known = FakeKnownPrincipals({"bob", "carol", SHARER.id})
            read_probe = FakeReadProbe({(SHARER.id, "d1")})
            pdp = StaticPdp(DecisionEffect.ALLOW)
            sink = RecordingAuditSink()
            svc = ShareService(client, known, read_probe, pdp, sink, settings=settings)

            await svc.share("d1", "user:bob", sharer=SHARER)

            resolver = OpenFgaSharedDocumentResolver(
                client, FakeGroupExpansion({}), known, settings
            )
            assert await resolver.shared_document_ids(Principal(id="bob")) == frozenset({"d1"})

            await svc.revoke("d1", "user:bob", actor=SHARER)  # real revoke
            await svc.revoke("d1", "user:bob", actor=SHARER)  # already-revoked: no-op
            await svc.revoke("d1", "user:carol", actor=SHARER)  # never-granted: no-op

            assert await resolver.shared_document_ids(Principal(id="bob")) == frozenset()

            shares = await svc.list_shares(document_id="d1")
            assert set(shares) == {(f"user:{SHARER.id}", "owner", "document:d1")}

            revoke_events = [e for e in sink.events if e.action == "rebac.share.revoked"]
            assert len(revoke_events) == 3

    asyncio.run(_body())


def test_live_non_absent_delete_error_not_swallowed() -> None:
    """REAL server: a DIFFERENT delete rejection is NOT a TupleAbsentError.

    The narrow-catch proof at the production boundary (C7), not inferred from
    the fake. OBSERVED verbatim (2026-07-24): deleting a tuple whose subject
    carries a colon-bearing group id reaches the server (a genuine round trip
    — HTTP 400, a request was answered) and is rejected with
    ``code="validation_error"``, ``error_message="Invalid tuple
    'document:d1#viewer@group:acl:tenant-all#member'. Reason: the 'user'
    field is malformed"`` — a DIFFERENT code and message than the
    absent-delete signature, so ``_is_absent_delete`` correctly returns
    ``False`` and the error surfaces as plain ``BackendError``.
    """

    async def _body() -> None:
        async with _live_client() as (client, _settings):
            with pytest.raises(BackendError) as excinfo:
                await client.delete_tuples(
                    [("group:acl:tenant-all#member", "viewer", "document:d1")]
                )
            assert not isinstance(excinfo.value, TupleAbsentError)

    asyncio.run(_body())


# --- resolver replay ----------------------------------------------------


def test_live_resolver_replay_direct_group_and_acl_strip() -> None:
    async def _body() -> None:
        async with _live_client() as (client, settings):
            known = FakeKnownPrincipals({"jane"})
            await client.write_tuples(
                [
                    ("user:jane", "viewer", "document:d1"),
                    ("group:g#member", "viewer", "document:d2"),
                ]
            )
            resolver = OpenFgaSharedDocumentResolver(
                client,
                FakeGroupExpansion({"jane": frozenset({"g", "acl:tenant-all"})}),
                known,
                settings,
            )
            visible = await resolver.shared_document_ids(Principal(id="jane"))
            assert visible == frozenset({"d1", "d2"})

            # OBSERVED (2026-07-24): the real server REJECTS a colon-bearing
            # group id ("group:acl:tenant-all#member") outright — HTTP 400
            # ValidationException, "the 'user' field is malformed" — mapped by
            # _client.py to BackendError. This is a SECOND, server-level
            # no-bypass layered on top of the resolver's own acl:* contextual-
            # tuple strip: even a caller who bypassed the resolver and wrote
            # the tuple directly could not smuggle an acl:* id into OpenFGA at
            # all. If a future OpenFGA version starts accepting such ids, the
            # fallback branch below still proves the real invariant: dSecret
            # must never resolve for jane.
            try:
                await client.write_tuples(
                    [("group:acl:tenant-all#member", "viewer", "document:dSecret")]
                )
            except BackendError:
                pass
            else:
                visible_after = await resolver.shared_document_ids(Principal(id="jane"))
                assert "dSecret" not in visible_after

    asyncio.run(_body())


def test_live_resolver_unknown_principal_gets_no_leg() -> None:
    async def _body() -> None:
        async with _live_client() as (client, settings):
            await client.write_tuples([("user:jane", "viewer", "document:d1")])
            resolver = OpenFgaSharedDocumentResolver(
                client, FakeGroupExpansion({}), FakeKnownPrincipals(set()), settings
            )
            visible = await resolver.shared_document_ids(Principal(id="jane"))
            assert visible == frozenset()

    asyncio.run(_body())


# --- ShareService end-to-end, real read-after-write --------------------


def test_live_share_service_end_to_end_read_after_write() -> None:
    """share -> immediate resolve -> revoke -> immediate ∅.

    The C7-union revocation-safety money assertion, replayed through the
    real client end to end.
    """

    async def _body() -> None:
        async with _live_client() as (client, settings):
            known = FakeKnownPrincipals({"bob", SHARER.id})
            read_probe = FakeReadProbe({(SHARER.id, "d1")})
            pdp = StaticPdp(DecisionEffect.ALLOW)
            sink = RecordingAuditSink()
            svc = ShareService(client, known, read_probe, pdp, sink, settings=settings)

            await svc.share("d1", "user:bob", sharer=SHARER)

            shares = await svc.list_shares(document_id="d1")
            assert set(shares) == {
                ("user:bob", "viewer", "document:d1"),
                (f"user:{SHARER.id}", "owner", "document:d1"),
            }

            resolver = OpenFgaSharedDocumentResolver(
                client, FakeGroupExpansion({}), known, settings
            )
            visible = await resolver.shared_document_ids(Principal(id="bob"))
            assert visible == frozenset({"d1"})

            await svc.revoke("d1", "user:bob", actor=SHARER)
            visible_after_revoke = await resolver.shared_document_ids(Principal(id="bob"))
            assert visible_after_revoke == frozenset()

    asyncio.run(_body())


def test_live_share_group_then_member_resolves() -> None:
    async def _body() -> None:
        async with _live_client() as (client, settings):
            known = FakeKnownPrincipals({"jane", "proj-x", SHARER.id})
            read_probe = FakeReadProbe({(SHARER.id, "d1")})
            pdp = StaticPdp(DecisionEffect.ALLOW)
            sink = RecordingAuditSink()
            svc = ShareService(client, known, read_probe, pdp, sink, settings=settings)

            await svc.share("d1", "group:proj-x", sharer=SHARER)

            resolver = OpenFgaSharedDocumentResolver(
                client, FakeGroupExpansion({"jane": frozenset({"proj-x"})}), known, settings
            )
            visible = await resolver.shared_document_ids(Principal(id="jane"))
            assert visible == frozenset({"d1"})

    asyncio.run(_body())


def test_live_pdp_deny_and_audit_failure_write_no_tuple() -> None:
    """DENY and an audit-append failure both leave zero tuples on the real server."""

    async def _body() -> None:
        async with _live_client() as (client, settings):
            known = FakeKnownPrincipals({"jane", SHARER.id})
            read_probe = FakeReadProbe({(SHARER.id, "d1")})

            deny_svc = ShareService(
                client,
                known,
                read_probe,
                StaticPdp(DecisionEffect.DENY),
                RecordingAuditSink(),
                settings=settings,
            )
            with pytest.raises(ShareNotPermittedError):
                await deny_svc.share("d1", "user:jane", sharer=SHARER)
            assert await client.read_tuples(object_filter="document:d1") == []

            audit_fail_svc = ShareService(
                client,
                known,
                read_probe,
                StaticPdp(DecisionEffect.ALLOW),
                FailingAuditSink(),
                settings=settings,
            )
            with pytest.raises(RebacAuditError):
                await audit_fail_svc.share("d1", "user:jane", sharer=SHARER)
            assert await client.read_tuples(object_filter="document:d1") == []

    asyncio.run(_body())
