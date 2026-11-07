# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Tests for ``ne.schema_bootstrap.report`` — the total-traceability report."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ne.schema_bootstrap.emit import emit_linkml
from ne.schema_bootstrap.errors import MentionCompletenessError
from ne.schema_bootstrap.infer import infer_candidate_model
from ne.schema_bootstrap.profiles import profile_samples
from ne.schema_bootstrap.report import (
    RefineLanding,
    expected_anchors,
    render_inference_report,
    verify_mention_completeness,
)


def _bundle_model_emitted(tmp_path: Path, *, engagement: str = "acme"):
    bundle = profile_samples(tmp_path)
    model = infer_candidate_model(bundle)
    emitted = emit_linkml(model, engagement=engagement)
    return bundle, model, emitted


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_report_mentions_every_emitted_element_exactly_once(tmp_path: Path) -> None:
    (tmp_path / "customers.csv").write_text(
        "id,name,email\n"
        + "\n".join(f"c{i},Cust {i},user{i}@example.com" for i in range(10))
        + "\n"
    )
    (tmp_path / "orders.csv").write_text(
        "id,customer_id,amount\n" + "\n".join(f"o{i},c{i % 10},{10 + i}" for i in range(10)) + "\n"
    )
    bundle, model, emitted = _bundle_model_emitted(tmp_path)
    report = render_inference_report(bundle.profile, model, emitted, engagement="acme")

    anchors = expected_anchors(model, emitted)
    assert anchors, "fixture must produce a non-empty anchor set (kills empty-vs-empty)"
    for anchor in anchors:
        if anchor.startswith("### "):
            count = len(re.findall(r"(?m)^" + re.escape(anchor) + r"$", report))
        else:
            count = report.count(anchor)
        assert count == 1, f"anchor {anchor!r} mentioned {count} times, expected exactly 1"

    # The self-check inside render_inference_report already passed (no raise).
    verify_mention_completeness(report, model, emitted)


def test_mention_check_goes_red_on_omission_and_duplication(tmp_path: Path) -> None:
    (tmp_path / "customers.csv").write_text(
        "id,name,email\n"
        + "\n".join(f"c{i},Cust {i},user{i}@example.com" for i in range(10))
        + "\n"
    )
    (tmp_path / "orders.csv").write_text(
        "id,customer_id,amount\n" + "\n".join(f"o{i},c{i % 10},{10 + i}" for i in range(10)) + "\n"
    )
    bundle, model, emitted = _bundle_model_emitted(tmp_path)
    report = render_inference_report(bundle.profile, model, emitted, engagement="acme")

    # (a) delete a class heading line -> MentionCompletenessError naming it.
    class_heading = "### Class `CustomersRecord`"
    (heading_line,) = [ln for ln in report.splitlines() if ln == class_heading]
    mutated_a = report.replace(heading_line + "\n", "", 1)
    with pytest.raises(MentionCompletenessError) as exc_a:
        verify_mention_completeness(mutated_a, model, emitted)
    assert "class CustomersRecord" in exc_a.value.missing

    # (b) delete a slot token -> named.
    slot_token = "`CustomersRecord.name`"
    assert slot_token in report
    mutated_b = report.replace(f"- {slot_token}", "- REMOVED-SLOT-MARKER", 1)
    with pytest.raises(MentionCompletenessError) as exc_b:
        verify_mention_completeness(mutated_b, model, emitted)
    assert "slot CustomersRecord.name" in exc_b.value.missing

    # (c) duplicate an edge token -> reported in duplicated.
    edge_token = "`OrdersRecord.customer_id -> CustomersRecord`"
    assert edge_token in report
    idx = report.index(edge_token)
    line_start = report.rfind("\n", 0, idx) + 1
    line_end = report.index("\n", idx)
    edge_line = report[line_start:line_end]
    mutated_c = report[:line_end] + "\n" + edge_line + report[line_end:]
    with pytest.raises(MentionCompletenessError) as exc_c:
        verify_mention_completeness(mutated_c, model, emitted)
    assert "edge OrdersRecord.customer_id->CustomersRecord" in exc_c.value.duplicated

    # (d) empty-string report vs non-empty model -> ALL anchors missing
    # (the deliberate-omission RED control the orchestrator required).
    with pytest.raises(MentionCompletenessError) as exc_d:
        verify_mention_completeness("", model, emitted)
    expected_labels = sorted(expected_anchors(model, emitted).values())
    assert exc_d.value.missing == expected_labels
    assert exc_d.value.duplicated == []


def test_untrusted_text_is_sanitized_in_serialized_report(tmp_path: Path) -> None:
    _write_jsonl(
        tmp_path / "widgets.jsonl",
        [
            {
                "id": f"w{i}",
                "hostile_header_ok": "benign value " + str(i),
            }
            for i in range(3)
        ],
    )
    # A hostile column HEADER (markdown heading + link + mention + issue ref)
    # and hostile exemplar VALUES (backtick runs, ANSI escapes, embedded
    # newlines) via a second table.
    header_row = '"# Heading [link](evil) @mention #123",note\n'
    rows = [header_row]
    hostile_value = "```danger\nrm -rf /\x1b[31m\x00``` and @mention #99"
    for i in range(3):
        rows.append(f'v{i},"{hostile_value.replace(chr(10), " ").replace(chr(34), chr(39))}"\n')
    (tmp_path / "hostile.csv").write_text("".join(rows))

    bundle, model, emitted = _bundle_model_emitted(tmp_path)
    report = render_inference_report(bundle.profile, model, emitted, engagement="acme")

    # No injected line-start heading exists anywhere in the report.
    assert not re.search(r"(?m)^# Heading", report)
    assert not re.search(r"(?m)^```danger", report)
    # Escapes applied to markdown-significant characters.
    assert "\\#" in report
    assert "\\@mention" in report or "\\@" in report
    # Non-printables gone.
    assert "\x1b" not in report
    assert "\x00" not in report

    # Benign control column renders its values recognizably.
    assert "benign value 0" in report


def test_pii_column_values_absent_from_serialized_report(tmp_path: Path) -> None:
    rows = ["id,dob,note"]
    for i in range(10):
        rows.append(f"c{i},199{i % 9}-0{(i % 9) + 1}-0{(i % 9) + 1},non-pii-note-{i}")
    (tmp_path / "customers.csv").write_text("\n".join(rows) + "\n")

    bundle, model, emitted = _bundle_model_emitted(tmp_path)
    report = render_inference_report(bundle.profile, model, emitted, engagement="acme")

    for i in range(9):
        assert f"199{i}-0" not in report  # no raw dob values anywhere

    assert "PII" in report
    assert "confirm with data owner" in report
    # Control: the non-PII column's exemplars ARE present.
    assert "non-pii-note-0" in report


def test_enum_divergence_disclosed(tmp_path: Path) -> None:
    shared_prefix = "A" * 80
    raw_a = shared_prefix + "-onetail"
    raw_b = shared_prefix + "-twotail"
    rows = ["id,tag"]
    for i in range(20):
        rows.append(f"w{i},{raw_a if i % 2 == 0 else raw_b}")
    (tmp_path / "widgets.csv").write_text("\n".join(rows) + "\n")

    bundle, model, emitted = _bundle_model_emitted(tmp_path)
    report = render_inference_report(bundle.profile, model, emitted, engagement="acme")

    assert "distinct (raw): 2" in report
    assert "permissible values (retained): 1" in report
    assert "collapsed by scrub/truncate" in report

    # No-collision control: no divergence note anywhere. Separate directory
    # so the collision fixture above doesn't leak into this model.
    control_dir = tmp_path / "control"
    control_dir.mkdir()
    rows2 = ["id,tag2"] + [f"w{i},val{i % 3}" for i in range(30)]
    (control_dir / "widgets2.csv").write_text("\n".join(rows2) + "\n")
    bundle2, model2, emitted2 = _bundle_model_emitted(control_dir)
    report2 = render_inference_report(bundle2.profile, model2, emitted2, engagement="acme")
    assert "collapsed by scrub/truncate" not in report2


def test_hostile_header_cannot_forge_an_anchor_token(tmp_path: Path) -> None:
    # Argus round-1 P2: a raw column header whose text, once safe_inline'd,
    # spells out ANOTHER real slot anchor exactly (`CustomersRecord.name`)
    # used to get backtick-wrapped by the renderer itself, forging a real
    # anchor token out of untrusted data and pushing that slot's true mention
    # count to 2 -> a spurious MentionCompletenessError("duplicated") on
    # perfectly legitimate data the operator did nothing wrong with.
    (tmp_path / "customers.csv").write_text(
        "id,name,email\n"
        + "\n".join(f"c{i},Cust {i},user{i}@example.com" for i in range(10))
        + "\n"
    )
    (tmp_path / "orders.csv").write_text(
        'id,customer_id,"CustomersRecord.name"\n'
        + "\n".join(f"o{i},c{i % 10},forged-{i}" for i in range(10))
        + "\n"
    )
    bundle, model, emitted = _bundle_model_emitted(tmp_path)

    # Must not raise MentionCompletenessError: render_inference_report calls
    # the self-check on its own output, so a forged anchor would surface
    # here as a spurious duplicate.
    report = render_inference_report(bundle.profile, model, emitted, engagement="acme")

    real_slot_anchor = "`CustomersRecord.name`"
    count = report.count(real_slot_anchor)
    assert count == 1, (
        f"real anchor {real_slot_anchor!r} mentioned {count} times — a hostile "
        "header forged an extra occurrence"
    )

    # The hostile header's own rendering is present but NOT backtick-wrapped
    # (double-quoted instead, per report.py's anchor-namespace-disjointness
    # fix) — so it can never be confused with a real anchor token.
    assert 'header: "CustomersRecord.name"' in report


def test_provenance_mapped_enum_is_not_rendered_as_an_enum_section(tmp_path: Path) -> None:
    # 2026-07-28 Argus P2: `source_system` is a PROVENANCE slot name
    # (`seeds.PROVENANCE_SLOTS`). A low-cardinality `source_system` column is
    # a perfectly plausible customer export column, and `infer.py` builds a
    # `CandidateEnum` for it regardless of the column's name — but
    # `emit._emit_class` skips provenance slots BEFORE `_slot_attribute` ever
    # records emitted/declined, so this enum is neither in
    # `emitted.emitted_enums` nor `emitted.declined_enums`. Before the fix,
    # the report's enum loop fell into its `else` branch and rendered a full
    # `### Enum` section (with PV bullets) for an enum the draft schema does
    # not actually carry.
    rows = ["id,source_system,name"]
    for i in range(30):
        rows.append(f"c{i},{'salesforce' if i % 2 == 0 else 'zendesk'},Cust {i}")
    (tmp_path / "customers.csv").write_text("\n".join(rows) + "\n")

    bundle, model, emitted = _bundle_model_emitted(tmp_path)
    assert emitted.emitted_enums == [], "fixture must produce zero EMITTED enums"
    assert emitted.declined_enums == {}, "fixture must produce zero DECLINED enums"
    provenance_only_enum = next(
        e for e in model.enums if "SourceSystem" in e.name
    )  # the enum the bug would have leaked into a `### Enum` section

    # render_inference_report calls verify_mention_completeness on its own
    # output — must NOT raise (proves the soundness fix doesn't just move
    # the bug into a self-check false-positive).
    report = render_inference_report(bundle.profile, model, emitted, engagement="acme")

    heading = f"### Enum `{provenance_only_enum.name}`"
    assert not re.search(r"(?m)^" + re.escape(heading) + r"$", report), (
        f"provenance-mapped-only enum {provenance_only_enum.name!r} was rendered as a "
        "full `### Enum` section — advertises a schema element the draft does not emit"
    )
    for pv in provenance_only_enum.permissible_values:
        assert f"\n- {pv}\n" not in report, (
            "provenance-mapped-only enum's permissible values leaked into the report body"
        )
    assert f"`{provenance_only_enum.name}` not emitted (column is provenance-mapped" in report

    # Control: a genuinely-emitted enum on the SAME model (there isn't one in
    # this fixture) is exercised by every other report test
    # (test_report_mentions_every_emitted_element_exactly_once, etc.) — this
    # test's own non-emptiness of `model.enums` alongside `emitted_enums ==
    # []` is itself the control that the omission-branch is real, not a
    # tautology from an empty enum list.
    assert model.enums, "fixture must actually produce a CandidateEnum to test the omission branch"


def test_mention_check_goes_red_on_an_unexpected_heading(tmp_path: Path) -> None:
    # 2026-07-28 Argus P2: completeness alone (every expected anchor present
    # exactly once) is blind to a report that ALSO advertises something
    # outside the expected set. Plant an anchor-shaped heading the model
    # never produced and confirm the self-check goes red naming it.
    (tmp_path / "widgets.csv").write_text("id,val\nw1,1\nw2,2\n")
    bundle, model, emitted = _bundle_model_emitted(tmp_path)
    report = render_inference_report(bundle.profile, model, emitted, engagement="acme")

    forged_class_heading = "### Class `ForgedClassNeverInModel`"
    mutated_class = report + "\n" + forged_class_heading + "\n\n- nothing\n"
    with pytest.raises(MentionCompletenessError) as exc_class:
        verify_mention_completeness(mutated_class, model, emitted)
    assert forged_class_heading in exc_class.value.unexpected

    forged_enum_heading = "### Enum `ForgedEnumNeverEmitted`"
    mutated_enum = report + "\n" + forged_enum_heading + "\n\n- nothing\n"
    with pytest.raises(MentionCompletenessError) as exc_enum:
        verify_mention_completeness(mutated_enum, model, emitted)
    assert forged_enum_heading in exc_enum.value.unexpected

    # Control: the unmodified report itself never trips the soundness check.
    verify_mention_completeness(report, model, emitted)


def test_report_is_deterministic_byte_identical(tmp_path: Path) -> None:
    # emit_linkml and render_pr_body each have a determinism test
    # (test_emit_is_deterministic_byte_identical,
    # test_prbody_is_deterministic); this is the report's, per the unit's
    # DoD claim ("identical inputs give byte-identical yaml/report/pr-body
    # across runs").
    (tmp_path / "customers.csv").write_text(
        "id,name,email\n"
        + "\n".join(f"c{i},Cust {i},user{i}@example.com" for i in range(10))
        + "\n"
    )
    bundle_a, model_a, emitted_a = _bundle_model_emitted(tmp_path)
    report_a = render_inference_report(bundle_a.profile, model_a, emitted_a, engagement="acme")

    bundle_b, model_b, emitted_b = _bundle_model_emitted(tmp_path)
    report_b = render_inference_report(bundle_b.profile, model_b, emitted_b, engagement="acme")

    assert report_a == report_b

    # Sensitivity control: a change to the underlying sample data (one more
    # row, changing "rows sampled"/exemplars) must change the rendered
    # report too, so the equality above isn't vacuous.
    (tmp_path / "customers.csv").write_text(
        "id,name,email\n"
        + "\n".join(f"c{i},Cust {i},user{i}@example.com" for i in range(11))
        + "\n"
    )
    bundle_c, model_c, emitted_c = _bundle_model_emitted(tmp_path)
    report_c = render_inference_report(bundle_c.profile, model_c, emitted_c, engagement="acme")
    assert report_c != report_a


def test_report_states_model_routing_and_thresholds(tmp_path: Path) -> None:
    from ne.schema_bootstrap.config import DEFAULT_CONFIG

    (tmp_path / "widgets.csv").write_text("id,val\nw1,1\nw2,2\n")
    bundle, model, emitted = _bundle_model_emitted(tmp_path)
    report = render_inference_report(bundle.profile, model, emitted, engagement="acme")

    assert "model routing: none (deterministic floor; LLM labeling is U5, opt-in)" in report
    for key, value in DEFAULT_CONFIG.model_dump().items():
        assert f"threshold {key}: {value}" in report


def _class_section(report: str, name: str) -> str:
    heading = f"### Class `{name}`"
    start = report.index(heading)
    rest = report[start + len(heading) :]
    match = re.search(r"(?m)^#", rest)
    end = start + len(heading) + (match.start() if match else len(rest))
    return report[start:end]


def test_landing_view_marks_statuses_and_survives_mention_check(tmp_path: Path) -> None:
    # 2026-07-29 round 4, Argus P2 fix: on `--refine`, the report must state
    # explicitly which draft each class/enum section is evidence for,
    # instead of silently describing this run's fresh machine draft next to
    # a landing draft that may have excluded/overridden it.
    (tmp_path / "customers.csv").write_text(
        "id,name,email\n"
        + "\n".join(f"c{i},Cust {i},user{i}@example.com" for i in range(10))
        + "\n"
    )
    (tmp_path / "orders.csv").write_text(
        "id,customer_id,amount\n" + "\n".join(f"o{i},c{i % 10},{10 + i}" for i in range(10)) + "\n"
    )
    (tmp_path / "products.csv").write_text(
        "id,name\n" + "\n".join(f"p{i},Product {i}" for i in range(5)) + "\n"
    )
    bundle, model, emitted = _bundle_model_emitted(tmp_path)
    class_names = sorted(c.name for c in model.classes)
    assert class_names == ["CustomersRecord", "OrdersRecord", "ProductsRecord"]

    landing = RefineLanding(
        class_names=["OrdersRecord", "ProductsRecord"],
        enum_names=[],
        tombstoned_classes=["CustomersRecord"],
        tombstoned_enums=[],
        human_kept=["classes.OrdersRecord"],
        machine_updated=[],
        blocked_resurrections=[],
        human_readded=[],
        orphaned_classes=[],
    )
    report = render_inference_report(
        bundle.profile, model, emitted, engagement="acme", landing=landing
    )

    assert report.count("## What landed (--refine)") == 1

    tombstoned_section = _class_section(report, "CustomersRecord")
    assert "- landing status: did NOT land — tombstoned" in tombstoned_section

    human_kept_section = _class_section(report, "OrdersRecord")
    assert "landed as the HUMAN'S version" in human_kept_section

    machine_landed_section = _class_section(report, "ProductsRecord")
    assert "landed (machine version)" in machine_landed_section

    # The self-check inside render_inference_report already ran (no raise);
    # re-running it here is the explicit, named assertion this test exists
    # to make.
    verify_mention_completeness(report, model, emitted)

    # Control: landing=None (the fresh-run default) carries zero landing
    # markers — byte-compatible with the pre-fix fresh path.
    control = render_inference_report(bundle.profile, model, emitted, engagement="acme")
    assert "landing status" not in control
    assert "What landed" not in control


def test_hostile_landing_name_cannot_forge_an_anchor_token(tmp_path: Path) -> None:
    # A human-typed landing class name that spells a REAL slot anchor's inner
    # text must not be able to create a second backticked occurrence of that
    # anchor — locks the double-quote (never backtick) delimiter decision for
    # landing names (see RefineLanding's docstring). A builder "simplifying"
    # the rendering to backtick-wrap landing names must turn this test RED.
    (tmp_path / "customers.csv").write_text(
        "id,name,email\n"
        + "\n".join(f"c{i},Cust {i},user{i}@example.com" for i in range(10))
        + "\n"
    )
    (tmp_path / "orders.csv").write_text(
        "id,customer_id,amount\n" + "\n".join(f"o{i},c{i % 10},{10 + i}" for i in range(10)) + "\n"
    )
    bundle, model, emitted = _bundle_model_emitted(tmp_path)
    real_anchor_inner = "CustomersRecord.name"  # a real `_slot_anchor` inner text
    baseline_report = render_inference_report(bundle.profile, model, emitted, engagement="acme")
    assert baseline_report.count(f"`{real_anchor_inner}`") == 1

    landing = RefineLanding(
        class_names=[c.name for c in model.classes] + [real_anchor_inner],
        enum_names=[],
        tombstoned_classes=[],
        tombstoned_enums=[],
        human_kept=[],
        machine_updated=[],
        blocked_resurrections=[],
        human_readded=[],
        orphaned_classes=[],
    )

    # Must not raise MentionCompletenessError: render_inference_report's own
    # self-check would surface a forged anchor as a spurious duplicate.
    report = render_inference_report(
        bundle.profile, model, emitted, engagement="acme", landing=landing
    )

    assert report.count(f"`{real_anchor_inner}`") == 1, (
        "the hostile landing name added a second backticked occurrence of a "
        "real anchor's inner text — it must be rendered double-quoted, never "
        "backtick-wrapped"
    )
    assert f'"{real_anchor_inner}"' in report
