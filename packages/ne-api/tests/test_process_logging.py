# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-API-LOGCONF — the shipped API process must actually EMIT its `ne.*` INFO.

``python -m ne.api`` is the container CMD (``deploy/docker/ne-app/Dockerfile``),
and every instrument under the ``ne.`` logger namespace — the retrieval
pipeline's timing line among them — reaches a handler only if something sets a
level for it. ``uvicorn.run`` does NOT: its ``log_config`` names the ``uvicorn*``
loggers and nothing else, so ``ne.*`` inherited root's WARNING and the API
process emitted nothing at all. ``ne.worker.main`` calls ``logging.basicConfig``,
which is why the worker emits ``INFO:ne.*`` lines and the API did not.

These tests pin the two halves of that: ``main`` configures the level BEFORE it
serves, and uvicorn's own ``log_config`` does not undo it afterwards.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys
import textwrap
from collections.abc import Iterator
from typing import Any

import pytest
import uvicorn

from ne.api import __main__ as api_main

_NE_INSTRUMENT = "ne.brain.pipeline"

#: The three ``ApiSettings`` fields that carry no default, so ``main`` can build
#: the REAL settings object rather than a stand-in. Inert placeholders: neither
#: test opens a connection to any of them.
_REQUIRED_API_ENV = {
    "NE_API_QDRANT_URL": "http://qdrant.invalid:6333",
    "NE_API_QDRANT_COLLECTION": "ne_chunks",
    "NE_API_TEI_URL": "http://tei.invalid:80",
}


@contextlib.contextmanager
def _fresh_process_logging() -> Iterator[None]:
    """Root logging as a FRESH interpreter has it: no handlers, WARNING.

    ``logging.basicConfig`` is a NO-OP when root already carries a handler, and
    under pytest it does — the harness attaches its capture handler for the call
    phase, i.e. after fixtures have run. So the reset has to happen here, in the
    test body, or the assertion would be made against pytest's configuration
    instead of the code under test. Saved and restored exactly, so the harness's
    own handler survives the trip.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    root.handlers[:] = []
    root.setLevel(logging.WARNING)
    try:
        yield
    finally:
        for handler in root.handlers[:]:
            root.removeHandler(handler)
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_main_configures_info_logging_for_the_ne_namespace_before_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``main`` enables INFO for ``ne.*`` and does it BEFORE ``uvicorn.run``.

    Ordering is the whole point: configuring after the serve call would take
    effect only once the server had already stopped, which is never.
    """
    for name, value in _REQUIRED_API_ENV.items():
        monkeypatch.setenv(name, value)
    observed: dict[str, Any] = {}

    def _fake_run(app: str, **kwargs: Any) -> None:
        instrument = logging.getLogger(_NE_INSTRUMENT)
        observed["app"] = app
        observed["log_config"] = kwargs.get("log_config")
        observed["ne_effective_level"] = logging.getLogger("ne").getEffectiveLevel()
        observed["instrument_emits_info"] = instrument.isEnabledFor(logging.INFO)
        observed["instrument_disabled"] = instrument.disabled
        observed["root_handlers"] = len(logging.getLogger().handlers)
        observed["ne_handlers"] = len(logging.getLogger("ne").handlers)
        observed["root_level"] = logging.getLogger().level
        observed["ne_propagates"] = logging.getLogger("ne").propagate

    # `ne.api.__main__` looks `run` up on the module at call time, so patching
    # it here is the same seam the process itself goes through.
    monkeypatch.setattr(uvicorn, "run", _fake_run)

    with _fresh_process_logging():
        api_main.main()

    assert observed["app"] == "ne.api.app:app_factory"
    # NE-API-LOGCONF is handed to uvicorn as `log_config`, NOT applied by hand in
    # `main`, and that distinction is the whole fix. `Config.configure_logging`
    # runs `dictConfig` in EVERY server process, so this survives a multi-worker
    # start; configuring in `main` alone does not, because
    # `uvicorn.config.Config.__init__` reads WEB_CONCURRENCY whenever `workers` is
    # not passed and the spawned workers import the app string without ever
    # running `main`. A Codex review caught that after an adversarial review had
    # rated the same thing merely latent. So these assertions read the CONFIG.
    config = observed["log_config"]
    assert isinstance(config, dict), "main must hand uvicorn an explicit log_config"
    ne_logger = config["loggers"]["ne"]
    assert ne_logger["level"] == "INFO"
    # Blast radius. The obvious spelling is `basicConfig(level=INFO)`, which is
    # what `ne.worker.main` does and what this first shipped as. It is wrong here:
    # a litellm Router lives in this process, attaches its OWN handler to the
    # `LiteLLM*` loggers and propagates, so a root handler at INFO both unmutes
    # litellm's INFO tier and prints every litellm record TWICE, errors included.
    # Reproduced directly. Docker's json-file driver here has no size cap.
    assert ne_logger["propagate"] is False, "ne must not reach a root handler twice"
    assert ne_logger["handlers"] == ["ne"], "ne needs its own handler, not root's"
    assert "root" not in config, "raising root would double every litellm record"
    # main itself must leave process-global logging alone.
    assert observed["root_handlers"] == 0
    assert observed["root_level"] == logging.WARNING


def test_uvicorn_log_config_does_not_undo_the_ne_namespace_level() -> None:
    """uvicorn's ``log_config`` cannot take the ``ne.*`` level back away.

    ``uvicorn.run`` applies ``LOGGING_CONFIG`` through ``dictConfig`` AFTER
    ``main`` has configured logging, so the ordering in ``main`` is only safe
    while that config leaves root alone: it carries no ``root`` key and sets
    ``disable_existing_loggers: false``, so root's level and root's handler both
    survive it and ``ne.*`` still propagates to a real stream.

    Run in a SUBPROCESS on purpose. ``dictConfig`` closes every handler in the
    interpreter's global handler list, which under pytest includes the harness's
    own — proving this in-process would corrupt logging for the rest of the
    session. A fresh interpreter also starts from the real WARNING default, which
    is the condition the shipped container actually boots in.
    """
    program = textwrap.dedent(
        """
        import logging, logging.config
        from uvicorn.config import LOGGING_CONFIG

        assert "root" not in LOGGING_CONFIG
        assert LOGGING_CONFIG["disable_existing_loggers"] is False
        assert logging.getLogger("ne.brain.pipeline").getEffectiveLevel() == logging.WARNING

        import ne.api.__main__ as api_main

        def _fake_run(app, **kwargs):
            # What uvicorn ACTUALLY does, in EVERY server process including each
            # spawned worker: Config.configure_logging applies the log_config it
            # was handed. Simulating that here is the point of this test -- a
            # worker never runs main(), so anything main() applied by hand would
            # be gone, and only what rides in log_config survives.
            logging.config.dictConfig(kwargs["log_config"])
            logging.getLogger("ne.brain.pipeline").info("NE-API-LOGCONF-PROOF")
            # litellm's shape: its own handler, propagating. Root must stay clean
            # so this emits ONCE and its INFO tier stays muted.
            logging.getLogger("LiteLLM").warning("LITELLM-WARNING-PROOF")
            logging.getLogger("LiteLLM").info("LITELLM-INFO-MUST-NOT-APPEAR")

        api_main.uvicorn.run = _fake_run
        api_main.main()
        """
    )
    completed = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, **_REQUIRED_API_ENV},
    )

    assert completed.returncode == 0, completed.stderr
    combined = completed.stderr + completed.stdout
    # The ne handler declared in log_config writes to stderr, and it got there
    # through uvicorn's own dictConfig -- which is what a spawned worker runs.
    assert "NE-API-LOGCONF-PROOF" in combined, combined
    assert "ne.brain.pipeline" in combined, combined
    # And the blast radius stays closed: litellm's INFO tier is still muted, so a
    # regression to basicConfig(level=INFO) reds this line.
    assert "LITELLM-INFO-MUST-NOT-APPEAR" not in combined, combined
