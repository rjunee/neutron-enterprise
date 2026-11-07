# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Pre-egress body redaction (NE-162) — the FRONTIER_API-only redaction walk.

Imports ONLY stdlib + :mod:`ne.contracts.governance` (the frozen ``Redactor``
Protocol + ``RedactionResult``): importing ``ne.govern`` here would break the
``ne162-gateway-redactor-protocol-seam`` import-linter contract (C3 chokepoint
opacity — ne-gateway must stay provider/backend-agnostic).

:func:`redact_payload` walks EVERY string leaf of the outbound body — the
system prompt, every message's content, any retrieved/RAG context embedded as
message content, tool schemas, tool-call arguments, tool results, and every
string-valued merged param — and returns brand-new copies. It is deliberately
NOT ``redactor.redact(json.dumps(body))``: redacting the serialized JSON
boundary risks corrupting the JSON structure itself (a replacement string
containing a quote or brace) and it also refuses to redact non-string leaves
that must survive untouched (numbers, booleans). A ``messages[].content``-only
walk is *also* rejected as a false promise: it would silently leave tool
schemas / tool-call arguments / tool results unredacted while claiming
"pre-egress redaction" ran.

This module never mutates its inputs — every test in
``test_redaction_seam.py`` asserts the shared ``messages`` / ``merged`` objects
built by the gateway are byte-identical after a redacted egress.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from typing import Any

from ne.contracts.governance import Redactor

# The one wire-protocol key inside a message dict that is NEVER redacted: it is
# a fixed enum ("system" / "user" / "assistant" / "tool"), never free text. A
# ``role`` key nested somewhere else in the payload (e.g. inside a tool-call
# argument) IS a normal string leaf and gets walked like everything else.
_MESSAGE_ROLE_KEY = "role"


def _walk(redactor: Redactor, value: Any, counter: Counter[str]) -> Any:
    if isinstance(value, str):
        result = redactor.redact(value)
        counter.update(result.redactions)
        return result.text
    if isinstance(value, Mapping):
        return {k: _walk(redactor, v, counter) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_walk(redactor, item, counter) for item in value]
    # int / float / bool / None / anything else: passed through untouched.
    return value


def _walk_message(
    redactor: Redactor, message: Mapping[str, Any], counter: Counter[str]
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in message.items():
        if key == _MESSAGE_ROLE_KEY:
            out[key] = value  # wire-protocol enum: preserved verbatim
        else:
            out[key] = _walk(redactor, value, counter)
    return out


def redact_payload(
    redactor: Redactor, messages: list[Any], params: Mapping[str, Any]
) -> tuple[list[Any], dict[str, Any], dict[str, int], int]:
    """Redact every string leaf of ``messages`` + ``params`` into fresh copies.

    Returns ``(new_messages, new_params, categories, total_hits)`` where
    ``categories`` is a ``{category: count}`` mapping sorted by category name
    and ``total_hits`` is the sum of all per-category counts across the entire
    body. Neither ``messages`` nor ``params`` (nor anything nested inside them)
    is mutated.
    """
    counter: Counter[str] = Counter()
    new_messages = [
        _walk_message(redactor, m, counter)
        if isinstance(m, Mapping)
        else _walk(redactor, m, counter)
        for m in messages
    ]
    new_params = {k: _walk(redactor, v, counter) for k, v in params.items()}
    categories = dict(sorted(counter.items()))
    total_hits = sum(counter.values())
    return new_messages, new_params, categories, total_hits
