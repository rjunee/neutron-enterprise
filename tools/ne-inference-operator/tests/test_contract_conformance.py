# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import cast

from ne.contracts.inference import (
    InferenceTargetOperation,
    InferenceTargetOperator,
    InferenceTargetState,
)
from ne.contracts.testing.kits.inference_operator import InferenceTargetOperatorContractKit
from ne.inference_operator.journal import StateJournal
from ne.inference_operator.operator import HostInferenceTargetOperator

from .test_host_operator import _FakeRunner, _manifest


class TestHostInferenceOperatorContract(InferenceTargetOperatorContractKit):
    target_id = "target-one"

    def create_operator(self) -> HostInferenceTargetOperator:
        directory = Path(tempfile.mkdtemp()).resolve()
        directory.chmod(0o700)
        operator = HostInferenceTargetOperator(
            _manifest(),
            StateJournal(directory / "state.jsonl", owner_uid=os.getuid()),
            runner=_FakeRunner(block=True),  # type: ignore[arg-type]
        )
        return operator

    def mutation_count(
        self,
        operator: InferenceTargetOperator,
        target_id: str,
        operation: InferenceTargetOperation,
    ) -> int:
        runner = cast(HostInferenceTargetOperator, operator)._runner
        assert isinstance(runner, _FakeRunner)
        return sum(candidate is operation for _adapter, candidate in runner.calls)

    def prepare_ready_target(self, operator: InferenceTargetOperator) -> None:
        cast(HostInferenceTargetOperator, operator)._states[self.target_id] = (
            InferenceTargetState.READY
        )
