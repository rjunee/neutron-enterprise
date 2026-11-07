# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Versioned judge rubrics (SPEC §5 NE-024 judge policy).

Rubrics are prompt-template files shipped as package data under
``ne/eval/rubrics/`` and indexed by ``rubrics.toml``. Each :class:`Rubric` carries
a ``content_hash`` = SHA-256 of the template file bytes; that hash binds
calibration artifacts to the exact template text.

Doc rule (also stated in the README): ANY change to a rubric's template text
requires a NEW version file (e.g. ``_v2.md``), a bump of the ``rubrics.toml``
entry, and a calibration re-run. The change is caught downstream by
``ne.eval.calibration.check_calibration`` comparing ``content_hash`` against the
hash recorded in each artifact — editing a rubric in place without recalibrating
fails the run closed.
"""

from __future__ import annotations

import hashlib
import tomllib
from functools import cache
from importlib.resources import files

from pydantic import BaseModel, ConfigDict

from ne.eval.errors import RubricError

_RUBRICS_DIR = "rubrics"
_INDEX_FILE = "rubrics.toml"


class Rubric(BaseModel):
    """A versioned, content-hashed judge prompt template."""

    model_config = ConfigDict(frozen=True)

    id: str
    version: str
    description: str
    template: str
    response_schema: str
    content_hash: str


def _load_index() -> tuple[Rubric, ...]:
    anchor = files("ne.eval").joinpath(_RUBRICS_DIR)
    index_text = anchor.joinpath(_INDEX_FILE).read_text(encoding="utf-8")
    parsed = tomllib.loads(index_text)
    entries = parsed.get("rubric", [])
    if not entries:
        raise RubricError(f"{_INDEX_FILE} declares no [[rubric]] entries")

    rubrics: list[Rubric] = []
    for entry in entries:
        try:
            rid = entry["id"]
            version = entry["version"]
            filename = entry["file"]
            description = entry["description"]
            response_schema = entry["response_schema"]
        except KeyError as exc:
            raise RubricError(f"rubric index entry missing key {exc}") from exc
        template_bytes = anchor.joinpath(filename).read_bytes()
        content_hash = hashlib.sha256(template_bytes).hexdigest()
        rubrics.append(
            Rubric(
                id=rid,
                version=version,
                description=description,
                template=template_bytes.decode("utf-8"),
                response_schema=response_schema,
                content_hash=content_hash,
            )
        )
    return tuple(rubrics)


@cache
def load_rubrics() -> tuple[Rubric, ...]:
    """Load and cache all packaged rubrics from ``rubrics.toml``."""
    return _load_index()


def get_rubric(rubric_id: str) -> Rubric:
    """Return the rubric with ``rubric_id``; raise :class:`RubricError` if unknown."""
    for rubric in load_rubrics():
        if rubric.id == rubric_id:
            return rubric
    known = sorted(r.id for r in load_rubrics())
    raise RubricError(f"unknown rubric {rubric_id!r}; known: {known}")


def render(rubric: Rubric, /, **variables: str) -> str:
    """Render ``rubric.template`` via ``str.format``; missing placeholder raises."""
    try:
        return rubric.template.format(**variables)
    except KeyError as exc:
        raise RubricError(f"rubric {rubric.id!r} render missing placeholder {exc}") from exc
