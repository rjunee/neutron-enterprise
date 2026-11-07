# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-FRONTIER-OUTPUT: a cut-off model response reaches the RUN saying it is one.

The gateway now carries ``Completion.finish_reason`` rather than discarding it
(``packages/ne-gateway``). This is the other end of that wire: pydantic-ai
already owns a slot for the fact — ``ModelResponse.finish_reason`` — which lands
in the run's own message history and on its span, so the run record itself
states that the assistant turn stopped at the output budget instead of finishing
its answer.

The pair, both directions:

* a truncated completion produces ``ModelResponse.finish_reason == "length"``;
* a normal one produces the byte-identical ``ModelResponse`` it produced before
  this unit — ``finish_reason`` stays ``None``, so nothing about a whole answer
  changed.

Only ``"length"`` is forwarded, deliberately: that slot is a pydantic-ai
``FinishReason`` Literal on ITS spellings, not the provider's, and translating
raw provider tokens into it would be inventing a mapping nobody stated.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters

from ne.contracts.models import Completion, Usage
from ne.runtime.config import RuntimeSettings
from ne.runtime.model import GatewayModel

from .conftest import ScriptedGateway


async def _unused_hook(name: str, args: Any) -> None:
    raise AssertionError("unknown-tool hook should not run")


def _completion(finish_reason: str | None) -> Completion:
    return Completion(
        text="Machine A's defect rate is 3.1%, machine B's is 4.7%, and machine C",
        model_id="stub",
        usage=Usage(prompt_tokens=3, completion_tokens=5),
        finish_reason=finish_reason,
    )


def _request(finish_reason: str | None) -> Any:
    gw = ScriptedGateway([_completion(finish_reason)])
    model = GatewayModel(gw, RuntimeSettings(), on_unknown_tool=_unused_hook)
    return asyncio.run(
        model.request(
            [ModelRequest(parts=[UserPromptPart(content="hi")])],
            None,
            ModelRequestParameters(),
        )
    )


def test_truncated_completion_marks_the_model_response() -> None:
    """Deleting the propagation -> RED: the run would record a cut-off answer as
    an ordinary one, which is the silent degrade this unit exists to end."""
    assert _request("length").finish_reason == "length"


def test_complete_response_is_unchanged() -> None:
    """THE PAIR. A whole answer's ModelResponse gains nothing — marking
    unconditionally, or forwarding the provider's token verbatim, -> RED."""
    assert _request("stop").finish_reason is None
    assert _request(None).finish_reason is None
