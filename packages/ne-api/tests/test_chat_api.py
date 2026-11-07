# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

from typing import Any

import pytest

from ne.api.routers import chat
from ne.runtime.factory import create_department_session, rehydrate_department_session

from .conftest import AppBuilder, get_state, run, text_completion


def test_chat_create_and_rehydrate_receive_the_canonical_source_guard(
    make_app: AppBuilder, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = make_app(completions=[text_completion("done")])
    expected = get_state(harness.app).source_guard
    assert expected is not None
    original_create = create_department_session
    original_rehydrate = rehydrate_department_session
    seen: list[Any] = []

    async def _create(*args: Any, **kwargs: Any):
        seen.append(kwargs.get("source_guard"))
        return await original_create(*args, **kwargs)

    async def _rehydrate(*args: Any, **kwargs: Any):
        seen.append(kwargs.get("source_guard"))
        return await original_rehydrate(*args, **kwargs)

    monkeypatch.setattr(chat, "create_department_session", _create)
    monkeypatch.setattr(chat, "rehydrate_department_session", _rehydrate)

    async def _drive() -> None:
        headers = harness.bearer("user:chat", groups=["engineering"], department="engineering")
        async with harness.client() as client:
            created = await client.post(
                "/api/v1/chat/sessions",
                json={"agent_name": "console-chat"},
                headers=headers,
            )
            assert created.status_code == 200
            session_id = created.json()["session_id"]
            turn = await client.post(
                f"/api/v1/chat/sessions/{session_id}/messages",
                json={"prompt": "hello"},
                headers=headers,
            )
            assert turn.status_code == 200

    run(_drive())
    assert seen == [expected, expected]
