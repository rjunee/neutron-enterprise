# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``ne.schema_bootstrap.prbody`` — the ``pr-body.md`` emitter."""

from __future__ import annotations

import re
from pathlib import Path

from ne.schema_bootstrap import sanitize
from ne.schema_bootstrap.emit import emit_linkml
from ne.schema_bootstrap.infer import infer_candidate_model
from ne.schema_bootstrap.prbody import render_pr_body
from ne.schema_bootstrap.profiles import profile_samples


def test_prbody_structure(tmp_path: Path) -> None:
    # 'mro' is probe-proven FATAL against the real generator (R5, EMPIRICAL —
    # collides with Enum.mro); the revised gate correctly ADMITS a backtick
    # value (see test_emit.py::test_enum_gate_widened_acceptance_set_emits),
    # so this fixture needed a genuinely-declining PV.
    rows = ["id,tag"]
    for i in range(20):
        tag = "mro" if i % 2 == 0 else "safe-tag"
        rows.append(f"w{i},{tag}")
    (tmp_path / "widgets.csv").write_text("\n".join(rows) + "\n")

    bundle = profile_samples(tmp_path)
    model = infer_candidate_model(bundle)
    emitted = emit_linkml(model, engagement="acme")
    pr_body = render_pr_body(model, emitted, engagement="acme", round_trip_verified=True)

    # H1.
    assert pr_body.startswith("# [schema-bootstrap] acme schema draft\n")

    # Metadata bullets.
    assert "- artifacts: schema-draft.yaml, inference-report.md" in pr_body
    assert "- classes: 1" in pr_body
    assert (
        "- round-trip: VERIFIED — `validate_draft_generates` ran and passed against "
        "the isolated ne-schema codegen venv" in pr_body
    )
    assert "- model routing: none (deterministic floor; LLM labeling is U5, opt-in)" in pr_body

    # Fenced YAML section, fence strictly longer than any backtick run in the
    # yaml_text (which itself never contains backticks, but the fence must
    # still exceed the yaml_text's own longest run, proven directly).
    assert "## schema-draft.yaml" in pr_body
    body_fence = sanitize.fence(emitted.yaml_text)
    assert f"{body_fence}yaml\n{emitted.yaml_text}\n{body_fence}" in pr_body
    longest_run_in_yaml = max((len(m) for m in re.findall("`+", emitted.yaml_text)), default=0)
    assert len(body_fence) > longest_run_in_yaml

    # Review notes: deviations present.
    assert "OperationalEntity" in pr_body
    assert "generate.py:211,215" in pr_body
    assert "seed anchors are reviewer proposals only" in pr_body
    assert "declined enum `WidgetsRecordTagEnum`" in pr_body
    assert "draft_graph.py" in pr_body and "generate.py:250-257" in pr_body

    # Tracked-manual-apply footer states the tool never applies/auto-lands.
    assert "## Tracked manual apply" in pr_body
    assert "NEVER applies, commits, or auto-lands the draft itself." in pr_body


def test_prbody_round_trip_line_reflects_only_what_the_caller_observed(tmp_path: Path) -> None:
    # 2026-07-28 Argus P2: `render_pr_body` never runs the round-trip itself
    # and must never claim it succeeded unless the CALLER says so. Proves
    # all three states render distinct, honest lines — and in particular
    # that the DEFAULT (no argument passed at all) is "not yet run", never
    # a silent "verified".
    (tmp_path / "widgets.csv").write_text("id,val\nw1,1\nw2,2\n")
    bundle = profile_samples(tmp_path)
    model = infer_candidate_model(bundle)
    emitted = emit_linkml(model, engagement="acme")

    verified = render_pr_body(model, emitted, engagement="acme", round_trip_verified=True)
    assert "- round-trip: VERIFIED" in verified
    assert "NOT VERIFIED" not in verified
    assert "NOT YET RUN" not in verified

    failed = render_pr_body(model, emitted, engagement="acme", round_trip_verified=False)
    assert "- round-trip: NOT VERIFIED" in failed
    assert "VERIFIED — `validate_draft_generates` ran and passed" not in failed

    not_run_explicit = render_pr_body(model, emitted, engagement="acme", round_trip_verified=None)
    assert "- round-trip: NOT YET RUN" in not_run_explicit

    # The default (no `round_trip_verified` argument at all) must match the
    # explicit `None` case exactly — a caller who forgets to thread the
    # evidence through gets the honest "not yet run" line, never a claim of
    # success by omission.
    default = render_pr_body(model, emitted, engagement="acme")
    assert default == not_run_explicit


def test_prbody_no_declined_enums_states_so(tmp_path: Path) -> None:
    (tmp_path / "widgets.csv").write_text("id,val\nw1,1\nw2,2\n")
    bundle = profile_samples(tmp_path)
    model = infer_candidate_model(bundle)
    emitted = emit_linkml(model, engagement="acme")
    pr_body = render_pr_body(model, emitted, engagement="acme")

    assert "- no enums were declined." in pr_body


def test_prbody_is_deterministic(tmp_path: Path) -> None:
    (tmp_path / "widgets.csv").write_text("id,val\nw1,1\nw2,2\n")
    bundle = profile_samples(tmp_path)
    model = infer_candidate_model(bundle)
    emitted = emit_linkml(model, engagement="acme")

    a = render_pr_body(model, emitted, engagement="acme")
    b = render_pr_body(model, emitted, engagement="acme")
    assert a == b
