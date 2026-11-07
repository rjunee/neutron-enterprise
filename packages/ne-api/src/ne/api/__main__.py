# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""``python -m ne.api`` — serve the composition root under uvicorn.

Multi-worker safe: all cross-request state lives in Postgres (sessions, approvals,
guardrail caps NE-070H) — the service is stateless per SPEC §4. Live-stack
acceptance (real Keycloak/OPA/Qdrant/Postgres) is parked to NE-103b/NE-077.
"""

from __future__ import annotations

import copy
from typing import Any, Final

import uvicorn
import uvicorn.config

from ne.api.settings import ApiSettings

#: NE-API-LOGCONF. Uvicorn's own logging config with the ``ne`` namespace added.
#:
#: Handed to ``uvicorn.run(log_config=...)`` rather than applied by hand, and that
#: is the whole point: ``Config.configure_logging`` runs ``dictConfig`` in EVERY
#: server process, so this survives a multi-worker start. Configuring logging in
#: ``main()`` alone does NOT -- ``uvicorn.config.Config.__init__`` reads
#: ``WEB_CONCURRENCY`` whenever ``workers`` is not passed
#: (``self.workers = int(os.environ["WEB_CONCURRENCY"])``), so multi-worker is a
#: SUPPORTED invocation of this entry point today, not a hypothetical one, and the
#: spawned workers import the app string without ever executing ``main()``.
#: A Codex review caught that; an adversarial review had rated the same thing
#: merely latent. It is not latent.
#:
#: Scoped deliberately to ``ne``: raising ROOT to INFO unmutes litellm's INFO tier
#: and DOUBLES every litellm record (its own handler plus root's), errors
#: included, because a litellm Router lives in this process and propagates.
#: ``propagate: False`` keeps ``ne`` lines single if root ever gains a handler.
_NE_LOG_CONFIG: Final[dict[str, Any]] = copy.deepcopy(uvicorn.config.LOGGING_CONFIG)
_NE_LOG_CONFIG["formatters"]["ne"] = {"format": "%(levelname)s:%(name)s:%(message)s"}
_NE_LOG_CONFIG["handlers"]["ne"] = {
    "class": "logging.StreamHandler",
    "formatter": "ne",
    "stream": "ext://sys.stderr",
}
_NE_LOG_CONFIG["loggers"]["ne"] = {"handlers": ["ne"], "level": "INFO", "propagate": False}


def main() -> None:
    settings = ApiSettings()  # type: ignore[call-arg]
    uvicorn.run(
        "ne.api.app:app_factory",
        factory=True,
        host=settings.bind_host,
        port=settings.bind_port,
        log_config=_NE_LOG_CONFIG,
    )


if __name__ == "__main__":
    main()
