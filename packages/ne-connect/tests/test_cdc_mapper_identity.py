# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""CDC item-identity recipe (NE-084): the DELETE-finds-its-chunk invariant."""

from __future__ import annotations

import re
from typing import Any

from ne.connect.cdc import CdcChange, CdcSinkConfig, CdcSinkService, CdcTableConfig
from ne.connect.cdc.config import CdcTableAclConfig

_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class _NullWriter:
    async def upsert(self, items: Any) -> Any:  # pragma: no cover - unused here
        raise NotImplementedError

    async def curate(self, *a: Any, **k: Any) -> None:  # pragma: no cover
        raise NotImplementedError

    async def quarantine(self, *a: Any, **k: Any) -> None:  # pragma: no cover
        raise NotImplementedError


class _NullQuarantine:
    def record(self, item: Any) -> None:  # pragma: no cover
        pass

    def get(self, item_id: str) -> Any:  # pragma: no cover
        return None

    def remove(self, item_id: str) -> None:  # pragma: no cover
        pass

    def list_quarantined(self) -> tuple[Any, ...]:  # pragma: no cover
        return ()


def _service() -> CdcSinkService:
    config = CdcSinkConfig(
        source_name="debezium",
        tables={
            "public.customers": CdcTableConfig(
                key_columns=("id",),
                content_columns=("name", "email"),
                acl=CdcTableAclConfig(principal_column="acl_group"),
            )
        },
    )
    return CdcSinkService(_NullWriter(), _NullQuarantine(), config)  # type: ignore[arg-type]


def _change(op: str, after: dict[str, Any] | None, before: dict[str, Any] | None) -> CdcChange:
    return CdcChange(
        op=op,
        table="public.customers",
        before=before,
        after=after,
        source={"schema": "public", "table": "customers"},
        ts_ms=1,
    )


def test_item_id_recipe_is_hex_sha256_no_colon() -> None:
    svc = _service()
    cfg = svc._config.tables["public.customers"]
    rid = svc._record_id({"id": 1, "name": "x"}, cfg)
    assert rid is not None
    item_id = svc._item_id("public.customers", rid)
    assert _HEX_SHA256.match(item_id)
    assert ":" not in item_id


def test_same_row_insert_then_update_same_item_id() -> None:
    svc = _service()
    cfg = svc._config.tables["public.customers"]
    insert = _change("c", {"id": 1, "name": "Ada", "acl_group": "g-eng"}, None)
    update = _change("u", {"id": 1, "name": "Ada2", "acl_group": "g-eng"}, {"id": 1})
    rid_i = svc._record_id(insert.after, cfg)
    rid_u = svc._record_id(update.after, cfg)
    assert rid_i is not None and rid_u is not None
    assert svc._item_id(insert.table, rid_i) == svc._item_id(update.table, rid_u)


def test_delete_finds_insert_item_id_from_before() -> None:
    svc = _service()
    cfg = svc._config.tables["public.customers"]
    insert_after = {"id": 7, "name": "Zoe", "acl_group": "g-eng"}
    delete_before = {"id": 7, "name": "Zoe", "acl_group": "g-eng"}
    rid_i = svc._record_id(insert_after, cfg)
    rid_d = svc._record_id(delete_before, cfg)
    assert rid_i is not None and rid_d is not None
    assert svc._item_id("public.customers", rid_i) == svc._item_id("public.customers", rid_d)


def test_missing_key_column_yields_no_record_id() -> None:
    svc = _service()
    cfg = svc._config.tables["public.customers"]
    assert svc._record_id({"name": "no id here"}, cfg) is None
    assert svc._record_id(None, cfg) is None


def test_build_item_hashes_record_id_and_projects_content() -> None:
    svc = _service()
    cfg = svc._config.tables["public.customers"]
    change = _change("c", {"id": 1, "name": "Ada", "email": "a@x", "acl_group": "g-eng"}, None)
    rid = svc._record_id(change.after, cfg)
    assert rid is not None
    item = svc._build_item(change, cfg, rid, svc._item_id(change.table, rid), ("g-eng",))
    # record_id in metadata is HASHED — no raw key value present.
    assert item.metadata["record_id"] != rid
    assert _HEX_SHA256.match(str(item.metadata["record_id"]))
    # content is the projected columns only (no acl_group, no id).
    assert "acl_group" not in item.content
    assert "Ada" in item.content
    assert item.acl == ("g-eng",)
