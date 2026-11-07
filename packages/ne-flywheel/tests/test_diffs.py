# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Renderer suite: typed proposals -> deterministic, injection-safe diff artifacts (NE-066).

Most tests build :class:`StoredFixProposal` fixtures inline (frozen models, no DB
needed) — the renderer is a PURE function. The structural test asserts the module
has no write capability by import.
"""

from __future__ import annotations

import hashlib
import inspect
import re
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

import ne.flywheel.diffs as diffs_module
from ne.flywheel.diffs import (
    DiffArtifactKind,
    ProposedFileChange,
    SourceAuthorityUnavailableError,
    render_proposal_diff,
)
from ne.flywheel.lanes import FixLane
from ne.flywheel.proposals import (
    GraphExtractionAssertion,
    GraphExtractionAssertionKind,
    GraphExtractionBatch,
    ProposalKind,
    StoredFixProposal,
)

#: The canonical injection payload: a GitHub issue-closing ref + mention, a fake
#: markdown heading, and a fake ```diff block that must all render INERT.
_ATTACK = "Fixes #123 @team\n## injected\n```diff\n--- a/x\n+++ b/x\n+evil\n```"

#: A fence line: a run of >=3 backticks or >=3 tildes, optionally followed by an
#: info string (which, for backtick fences, may not itself contain a backtick).
_FENCE_RE = re.compile(r"^(?P<marks>`{3,}|~{3,})(?P<info>[^`]*)$")


def _expected_name(proposal_id: str) -> str:
    return hashlib.sha256(proposal_id.encode()).hexdigest()[:16]


def _headings_outside_fences(md: str) -> list[str]:
    """Collect markdown-heading lines ('#'-prefixed) that sit OUTSIDE code fences.

    Walks lines tracking fence state: a fence opens on a >=3 backtick/tilde run
    and closes only on a same-character run of length >= the opening length with
    an empty info string. Lines starting with '#' while inside a fence are inert
    and are not collected.
    """
    headings: list[str] = []
    open_char = ""
    open_len = 0
    for raw in md.splitlines():
        m = _FENCE_RE.match(raw.strip())
        if m:
            char = m.group("marks")[0]
            length = len(m.group("marks"))
            info = m.group("info").strip()
            if open_char:
                if char == open_char and length >= open_len and info == "":
                    open_char = ""
                    open_len = 0
                continue
            open_char = char
            open_len = length
            continue
        if not open_char and raw.strip().startswith("#"):
            headings.append(raw)
    return headings


def _toplevel_diff_fence_next_lines(md: str) -> list[str]:
    """For every TOP-LEVEL fence opening whose info string is 'diff', return the
    line immediately following it (used to prove the real patch, not a fake one,
    lives inside the single 'diff'-tagged fence)."""
    lines = md.splitlines()
    result: list[str] = []
    open_char = ""
    open_len = 0
    for i, raw in enumerate(lines):
        m = _FENCE_RE.match(raw.strip())
        if not m:
            continue
        char = m.group("marks")[0]
        length = len(m.group("marks"))
        info = m.group("info").strip()
        if open_char:
            if char == open_char and length >= open_len and info == "":
                open_char = ""
                open_len = 0
            continue
        if info == "diff":
            result.append(lines[i + 1] if i + 1 < len(lines) else "")
        open_char = char
        open_len = length
    return result


def _proposal(
    *,
    lane: str = "content",
    kind: str = "fix",
    summary: str = "the produced answer is wrong; propose a source-document fix",
    payload: dict[str, Any] | None = None,
    proposal_id: str = "prop-0001",
) -> StoredFixProposal:
    return StoredFixProposal(
        id=1,
        proposal_id=proposal_id,
        kind=kind,
        lane=lane,
        cluster_id="cluster-xyz",
        status="proposed",
        config_hash="cfg-hash-abc",
        summary=summary,
        confidence=0.7,
        priority="medium",
        evidence_event_ids=("evt-1", "evt-2"),
        evidence_signal_ids=(2, 3),
        payload=payload or {},
        created_at=datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC),
    )


def _hunk_lines_valid(patch: str) -> bool:
    """Every body line of a unified diff starts with +/-/space/@@ (or is a header)."""
    for line in patch.splitlines():
        if line.startswith(("--- ", "+++ ")):
            continue
        if line and line[0] not in {"+", "-", " ", "@"}:
            return False
    return True


def test_content_lane_renders_git_unified_diff() -> None:
    artifact = render_proposal_diff(_proposal(lane=FixLane.CONTENT.value))
    assert artifact.kind is DiffArtifactKind.GIT_UNIFIED_DIFF
    assert artifact.patch.startswith("--- /dev/null\n")
    assert "+++ b/proposals/content/" in artifact.patch
    assert artifact.path.startswith("proposals/content/")
    assert artifact.path.endswith(".md")
    assert "@@ " in artifact.patch
    assert _hunk_lines_valid(artifact.patch)


@pytest.mark.parametrize("lane", [FixLane.SCHEMA, FixLane.RETRIEVAL])
def test_schema_and_retrieval_lanes_render_git_diff(lane: FixLane) -> None:
    artifact = render_proposal_diff(_proposal(lane=lane.value))
    assert artifact.kind is DiffArtifactKind.GIT_UNIFIED_DIFF
    assert artifact.path.startswith(f"proposals/{lane.value}/")
    assert f"+++ b/proposals/{lane.value}/" in artifact.patch
    assert _hunk_lines_valid(artifact.patch)


def test_skills_lane_renders_via_generic_auto_trusted_path() -> None:
    # The SKILLS lane is auto-trusted by _KNOWN_LANES (derived from the enum), so
    # render_proposal_diff never raises the unknown-lane ValueError — it renders
    # the generic git-unified diff exactly like the other job-emitted lanes.
    artifact = render_proposal_diff(
        _proposal(lane=FixLane.SKILLS.value, kind=ProposalKind.SKILL_CANDIDATE.value)
    )
    assert artifact.kind is DiffArtifactKind.GIT_UNIFIED_DIFF
    assert artifact.path.startswith("proposals/skills/")


def test_policy_lane_renders_opa_policy_diff() -> None:
    artifact = render_proposal_diff(_proposal(lane=FixLane.POLICY.value))
    assert artifact.kind is DiffArtifactKind.OPA_POLICY_DIFF
    assert artifact.path.endswith("_review.rego")
    # Every ADDED content line (not the +++ header) is a rego comment.
    for line in artifact.patch.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            assert line[1:].startswith("#"), f"non-comment rego line: {line!r}"


def test_golden_set_candidate_renders_candidate_payload() -> None:
    payload = {
        "candidate_id": "prop-gsc",
        "question_ref": "q-shared",
        "wrong_source_refs": ["cite-1", "cite-2"],
        "expected_source_refs": [],
        "evidence_event_ids": ["evt-1"],
        "notes": "Steward resolves the expected source at review.",
    }
    artifact = render_proposal_diff(
        _proposal(
            lane=FixLane.RETRIEVAL.value,
            kind=ProposalKind.GOLDEN_SET_CANDIDATE.value,
            payload=payload,
        )
    )
    assert "question_ref: q-shared" in artifact.pr_body
    assert "wrong_source_refs: cite-1, cite-2" in artifact.pr_body
    # expected_source_refs is EMPTY in v1 (steward resolves it at review).
    assert "- expected_source_refs: \n" in artifact.pr_body
    assert "Steward resolves the expected source at review." in artifact.pr_body


def test_renderer_is_deterministic() -> None:
    proposal = _proposal()
    first = render_proposal_diff(proposal)
    second = render_proposal_diff(proposal)
    assert first == second
    assert first.patch == second.patch
    assert first.pr_body == second.pr_body


def test_malicious_summary_cannot_escape_rego_comments() -> None:
    malicious = "benign line\npackage evil\ndefault allow = true"
    artifact = render_proposal_diff(_proposal(lane=FixLane.POLICY.value, summary=malicious))
    added = [
        line[1:]
        for line in artifact.patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    assert added, "expected added rego lines"
    for content in added:
        assert content.startswith("#"), f"executable rego escaped a comment: {content!r}"
    # The injected rego directive exists only INSIDE a comment.
    assert any("default allow = true" in c for c in added)
    assert not any(c.strip().startswith("default allow") for c in added)


def test_malicious_summary_cannot_break_markdown_fence() -> None:
    # A summary containing a 3- and a 4-backtick run must be wrapped in a longer fence.
    malicious = "start ``` then ```` end"
    artifact = render_proposal_diff(_proposal(summary=malicious))
    # The fence used around the summary must be >= 5 backticks (longest run + 1).
    assert "`````" in artifact.pr_body
    # The summary text is present but wrapped, not breaking out.
    assert malicious in artifact.pr_body


def test_concrete_change_renders_before_after_diff() -> None:
    change = ProposedFileChange(
        path="docs/runbook.md",
        before_text="step one\nstep two\nstep three\n",
        after_text="step one\nstep 2\nstep three\n",
    )
    artifact = render_proposal_diff(_proposal(), change=change)
    assert artifact.path == "docs/runbook.md"
    assert "--- a/docs/runbook.md" in artifact.patch
    assert "+++ b/docs/runbook.md" in artifact.patch
    assert "-step two" in artifact.patch
    assert "+step 2" in artifact.patch


@pytest.mark.parametrize(
    "bad_path",
    [
        "../../etc/passwd",
        "/abs/path",
        "a\\b",
        "x/../y",
        "",
        "docs/a\nb.md",
        "docs/a\tb.md",
        "docs/a\x1bb.md",
    ],
)
def test_change_path_traversal_rejected(bad_path: str) -> None:
    with pytest.raises(ValidationError):
        ProposedFileChange(path=bad_path, after_text="x")


def test_renderer_module_is_structurally_write_free() -> None:
    source = inspect.getsource(diffs_module)
    assert "sqlalchemy" not in source
    assert "httpx" not in source
    # No engine/connection parameter on the public renderer.
    params = set(inspect.signature(render_proposal_diff).parameters)
    assert "engine" not in params
    assert "conn" not in params
    assert "connection" not in params


# --------------------------------------------------------------------------- #
# Injection-safety attack suite (NE-066 continuation): every proposal-carried
# free-text vector renders INERT in the steward's decision artifact.
# --------------------------------------------------------------------------- #


def _golden_attack_proposal() -> StoredFixProposal:
    return _proposal(
        lane=FixLane.RETRIEVAL.value,
        kind=ProposalKind.GOLDEN_SET_CANDIDATE.value,
        payload={
            "question_ref": "q1\n## injected\nFixes #123 @team",
            "wrong_source_refs": ["cite\n```diff\n+boom", "ok-ref"],
            "expected_source_refs": ["exp\n# h1 @root"],
            "notes": "note ```` runs",
        },
    )


@pytest.mark.parametrize(
    "lane",
    [FixLane.CONTENT, FixLane.RETRIEVAL, FixLane.SCHEMA, FixLane.POLICY],
)
def test_title_and_h1_derive_only_from_trusted_fields(lane: FixLane) -> None:
    artifact = render_proposal_diff(_proposal(lane=lane.value, summary=_ATTACK))
    name = _expected_name("prop-0001")
    assert artifact.title == f"[flywheel:{lane.value}] proposal {name}"
    assert "Fixes" not in artifact.title
    assert "#123" not in artifact.title
    assert "@" not in artifact.title
    assert "\n" not in artifact.title
    assert artifact.pr_body.splitlines()[0] == f"# {artifact.title}"


def test_malicious_summary_headings_and_fake_diff_render_inert() -> None:
    artifact = render_proposal_diff(_proposal(lane=FixLane.CONTENT.value, summary=_ATTACK))
    headings = _headings_outside_fences(artifact.pr_body)
    assert not any("injected" in h for h in headings)
    diff_nexts = _toplevel_diff_fence_next_lines(artifact.pr_body)
    assert len(diff_nexts) == 1
    assert diff_nexts[0].startswith("--- ")
    # difflib double-prefixes the fake hunk line ('+evil' -> '++evil'), so it can
    # never masquerade as a real added-content line.
    assert "\n+evil\n" not in "\n" + artifact.patch
    assert _hunk_lines_valid(artifact.patch)


def test_malicious_golden_refs_render_inert_in_pr_body() -> None:
    artifact = render_proposal_diff(_golden_attack_proposal())
    body = artifact.pr_body
    ref_lines = [line for line in body.splitlines() if line.startswith("- question_ref: ")]
    assert len(ref_lines) == 1
    assert "injected" in ref_lines[0]
    assert r"Fixes \#123" in body
    assert "Fixes #123" not in body
    assert r"\@team" in body
    assert not any("injected" in h or "h1" in h for h in _headings_outside_fences(body))
    assert "ok-ref" in body


def test_malicious_golden_refs_render_inert_in_review_document() -> None:
    artifact = render_proposal_diff(_golden_attack_proposal())
    doc = "\n".join(
        line[1:]
        for line in artifact.patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    name = _expected_name("prop-0001")
    assert set(_headings_outside_fences(doc)) == {
        f"# Proposal review {name}",
        "## Summary (proposal-carried, untrusted)",
        "## Golden-set candidate",
        "### Steward resolution note",
    }


def test_policy_lane_malicious_summary_trusted_title_and_comment_only() -> None:
    artifact = render_proposal_diff(_proposal(lane=FixLane.POLICY.value, summary=_ATTACK))
    name = _expected_name("prop-0001")
    assert artifact.title == f"[flywheel:policy] proposal {name}"
    for line in artifact.patch.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            assert line[1:].startswith("#"), f"non-comment rego line: {line!r}"
    assert not any("injected" in h for h in _headings_outside_fences(artifact.pr_body))


def test_metadata_fields_with_newlines_render_single_line() -> None:
    proposal = StoredFixProposal(
        id=1,
        proposal_id="prop-0001",
        kind="fix",
        lane="content",
        cluster_id="c1\n## injected",
        status="proposed",
        config_hash="h\n# x",
        summary="benign",
        confidence=0.7,
        priority="medium",
        evidence_event_ids=("e\n## z",),
        evidence_signal_ids=(2,),
        payload={},
        created_at=datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC),
    )
    artifact = render_proposal_diff(proposal)
    body = artifact.pr_body
    for prefix in ("- cluster_id:", "- config_hash:", "- evidence_event_ids:"):
        bullets = [line for line in body.splitlines() if line.startswith(prefix)]
        assert len(bullets) == 1, f"{prefix} spilled to multiple lines"
    assert not any("injected" in h or h.strip() == "# z" for h in _headings_outside_fences(body))
    assert re.fullmatch(r"proposals/content/[0-9a-f]{16}\.md", artifact.path)


def test_unknown_lane_rejected() -> None:
    with pytest.raises(ValueError, match="lane"):
        render_proposal_diff(_proposal(lane="../../evil"))


def test_renderer_deterministic_on_malicious_input() -> None:
    proposal = _golden_attack_proposal()
    first = render_proposal_diff(proposal)
    second = render_proposal_diff(proposal)
    assert first == second


def test_safe_escapes_markdown_and_collapses_whitespace() -> None:
    assert diffs_module._safe("a\r\nb\tc\x1bd") == "a b cd"
    assert diffs_module._safe("#1 @u [x](y) `z` <s>") == r"\#1 \@u \[x\]\(y\) \`z\` \<s\>"


def test_safe_identity_on_benign_refs() -> None:
    assert diffs_module._safe("cite-1, q-shared: ok.") == "cite-1, q-shared: ok."


def _entity_merge_proposal(*, left_display: str = "Acme Inc") -> StoredFixProposal:
    return _proposal(
        lane="entity_resolution",
        kind="entity_merge_candidate",
        summary="candidate merge over blocking rule email_exact",
        proposal_id="sha256:er-abc",
        payload={
            "left": {"entity_class": "Account", "entity_key": "k-left", "display": left_display},
            "right": {"entity_class": "Account", "entity_key": "k-right", "display": "Acme"},
            "match_probability": 0.987,
            "match_weight": 12.5,
            "threshold": 0.95,
            "blocking_rule": "l.email = r.email",
        },
    )


def test_entity_merge_candidate_renders_entity_pair_preview() -> None:
    artifact = render_proposal_diff(_entity_merge_proposal())
    body = artifact.pr_body
    assert "## Entity-merge candidate" in body
    assert "left entity_key: k\\-left" in body or "left entity_key: k-left" in body
    assert "right entity_class: Account" in body
    assert "match_probability: 0.987" in body
    assert "blocking_rule: l.email = r.email" in body
    # The .md review document (git lane) carries the branch too.
    assert "## Entity-merge candidate" in artifact.patch


def test_entity_merge_candidate_is_deterministic() -> None:
    proposal = _entity_merge_proposal()
    assert render_proposal_diff(proposal) == render_proposal_diff(proposal)


def test_entity_merge_display_injection_is_escaped() -> None:
    attack = "# pwn\n## fake heading [x](http://evil) `code`"
    artifact = render_proposal_diff(_entity_merge_proposal(left_display=attack))
    body = artifact.pr_body
    # The hostile display name never becomes a real heading or link: newlines are
    # collapsed and markdown-significant characters are backslash-escaped.
    assert "\n# pwn" not in body
    assert "left display: \\# pwn \\#\\# fake heading" in body
    assert "[x](http://evil)" not in body


# --- NE-ONTOLOGY-FLYWHEEL: verified-answer-template proposals ----------------


_TEMPLATE_TOML = """template_id = "accounts-directory"
version = 2
description = "Every account visible to the caller."
sparql = \"\"\"
SELECT ?account ?name WHERE {
  #BINDINGS#
  ?account <https://x/name> ?name .
}
\"\"\"
[scope]
graph_classes = ["alpha"]
result_shape = "enumerable"
[answer]
columns = ["account", "name"]
citation_var = "account"
"""


def _verified_proposal(
    *, template_toml: str = _TEMPLATE_TOML, summary: str = "add accounts-directory v2"
) -> StoredFixProposal:
    return _proposal(
        lane=FixLane.ONTOLOGY.value,
        kind=ProposalKind.VERIFIED_ANSWER_CANDIDATE.value,
        summary=summary,
        proposal_id="sha256:verified-abc",
        payload={
            "template_id": "accounts-directory",
            "version": 2,
            "toml_sha256": hashlib.sha256(template_toml.encode()).hexdigest(),
            "template_toml": template_toml,
        },
    )


def test_verified_answer_kind_renders_fenced_toml() -> None:
    artifact = render_proposal_diff(_verified_proposal())
    # A verified_answer_candidate on the ontology lane is a git-unified-diff artifact
    # at proposals/ontology/<sha16>.md (non-policy → automatic).
    assert artifact.kind is DiffArtifactKind.GIT_UNIFIED_DIFF
    assert artifact.path == f"proposals/ontology/{_expected_name('sha256:verified-abc')}.md"
    body = artifact.pr_body
    assert "## Verified-answer template (steward-authored)" in body
    # '-' is NOT inline-escaped (only line-start-significant, already neutralized).
    assert "- template_id: accounts-directory" in body
    assert "- version: 2" in body
    # The TOML body appears inside a fenced ```toml block (present in both the PR
    # body and the .md review document).
    assert "```toml" in body
    assert "```toml" in artifact.patch
    assert 'template_id = "accounts-directory"' in body
    # Deterministic (pure function).
    assert render_proposal_diff(_verified_proposal()) == artifact


def test_verified_answer_hostile_toml_cannot_escape_fence() -> None:
    # A hostile TOML body with a backtick run + a markdown heading + a
    # summary that itself tries to inject a heading — none may escape (C12).
    hostile_toml = _TEMPLATE_TOML + '\n# ```\n## injected heading\nx = "````"\n'
    artifact = render_proposal_diff(
        _verified_proposal(template_toml=hostile_toml, summary="## fake\n```\nevil")
    )
    # No heading the payload/summary planted appears OUTSIDE a fenced block: the
    # hostile TOML + summary are fully contained inside their dynamic fences.
    headings = _headings_outside_fences(artifact.pr_body)
    assert not any("injected heading" in h for h in headings)
    assert not any(h.strip() == "## fake" for h in headings)
    # Every heading outside a fence is renderer-authored (trusted, starts with '#').
    for h in headings:
        assert h.lstrip().startswith("#")
    # The hostile TOML text is still present — but only INSIDE a fenced block.
    assert "## injected heading" in artifact.pr_body


def test_ontology_lane_is_trusted() -> None:
    # The 'ontology' lane passes the _KNOWN_LANES allowlist (derives from FixLane)
    # and renders without ValueError.
    artifact = render_proposal_diff(_verified_proposal())
    assert artifact.lane == "ontology"
    assert "[flywheel:ontology]" in artifact.title


# --- NE-T2X-1: graph_extraction_candidate batch diff rendering --------------


def _extraction_batch_payload(**overrides: Any) -> dict[str, Any]:
    batch = GraphExtractionBatch(
        document_ref=overrides.get("document_ref", "doc:doc-9"),
        run_id=overrides.get("run_id", "run-1"),
        source_system=overrides.get("source_system", "sharepoint"),
        assertions=(
            GraphExtractionAssertion(
                row_id="row-link-1",
                kind=GraphExtractionAssertionKind.LINK,
                entity_class="Account",
                entity_key="k-acme",
                confidence=0.93,
                evidence_ref="chunk:doc-9:c3",
            ),
            GraphExtractionAssertion(
                row_id="row-entity-1",
                kind=GraphExtractionAssertionKind.NEW_ENTITY,
                entity_class="Account",
                entity_key=overrides.get("entity_key", "ne:llm:globex-corp"),
                display=overrides.get("display", "Globex Corp"),
                properties=overrides.get("properties", {"domain": "globex.example"}),
                confidence=0.81,
                evidence_ref="chunk:doc-9:c4",
            ),
            GraphExtractionAssertion(
                row_id="row-edge-1",
                kind=GraphExtractionAssertionKind.NEW_EDGE,
                subject_key="k-acme",
                edge_kind="PARTNERS_WITH",
                object_key="k-globex",
                confidence=0.77,
                evidence_ref="chunk:doc-9:c4",
            ),
        ),
    )
    return batch.model_dump(mode="json")


def _graph_extraction_proposal(**overrides: Any) -> StoredFixProposal:
    payload = _extraction_batch_payload(**overrides)
    return _proposal(
        lane="entity_resolution",
        kind="graph_extraction_candidate",
        summary="graph-extraction batch over doc:doc-9 (3 assertions)",
        proposal_id="sha256:extract-abc",
        payload=payload,
    )


def test_source_bearing_graph_proposal_refuses_render_without_current_authority() -> None:
    proposal = _graph_extraction_proposal()
    proposal = proposal.model_copy(
        update={"payload": {**proposal.payload, "source_authority_stamps": [{"opaque": True}]}}
    )
    with pytest.raises(SourceAuthorityUnavailableError):
        render_proposal_diff(proposal)
    artifact = render_proposal_diff(proposal, source_authority_authorized=True)
    assert "source_authority_stamps" not in artifact.pr_body


def test_graph_extraction_batch_renders_per_row_blocks() -> None:
    artifact = render_proposal_diff(_graph_extraction_proposal())
    # pr_body embeds the review document TWICE (once directly, once nested
    # inside the fenced 'diff' patch block) -- so count real (unfenced)
    # headings via the module's fence-walking helper, the same discipline the
    # entity-merge/golden-set injection suite uses.
    pr_headings = _headings_outside_fences(artifact.pr_body)
    pr_row_headings = [h for h in pr_headings if h.startswith("### Row")]
    assert len(pr_row_headings) == 3
    # patch is the review document's OWN unified diff -- no nesting, exactly 3.
    assert artifact.patch.count("### Row ") == 3
    for body in (artifact.pr_body, artifact.patch):
        assert "## Graph-extraction batch (per-document)" in body
        assert "document_ref: doc:doc-9" in body
        # THREE separate task-list lines (each a real, independently
        # tickable GFM checkbox) -- not one line with two inert trailers.
        assert "- [ ] approve" in body
        assert "- [ ] edit (note the correction)" in body
        assert "- [ ] decline" in body
        assert "kind: link" in body
        # '_' is in the inline-escape set, so 'new_entity'/'new_edge' render
        # backslash-escaped (the same discipline as PARTNERS_WITH below).
        assert "kind: new\\_entity" in body
        assert "kind: new\\_edge" in body
        assert "evidence_ref: chunk:doc-9:c3" in body
        assert "evidence_ref: chunk:doc-9:c4" in body
        assert "confidence: 0.93" in body
        assert "display: Globex Corp" in body
        assert "prop domain: globex.example" in body
        assert "subject_key: k-acme" in body
        assert "edge_kind: PARTNERS\\_WITH" in body
        assert "object_key: k-globex" in body
        # Never-understate discipline (Argus round-2 P2): a fully-clean batch
        # shows explicitly that nothing was dropped, not merely omit the line.
        assert "- unrenderable rows: 0" in body


def test_graph_extraction_batch_is_deterministic() -> None:
    proposal = _graph_extraction_proposal()
    assert render_proposal_diff(proposal) == render_proposal_diff(proposal)


def test_graph_extraction_lane_and_path() -> None:
    artifact = render_proposal_diff(_graph_extraction_proposal())
    assert artifact.kind is DiffArtifactKind.GIT_UNIFIED_DIFF
    assert artifact.lane == "entity_resolution"
    expected_name = _expected_name("sha256:extract-abc")
    assert artifact.path == f"proposals/entity_resolution/{expected_name}.md"
    assert artifact.title.startswith("[flywheel:entity_resolution]")


def test_graph_extraction_display_injection_is_escaped() -> None:
    attack = "# pwn\n## fake heading [x](http://evil) `code`"
    artifact = render_proposal_diff(
        _graph_extraction_proposal(display=attack, properties={"note": attack})
    )
    body = artifact.pr_body
    headings = _headings_outside_fences(body)
    assert not any("fake heading" in h for h in headings)
    assert "[x](http://evil)" not in body
    assert "\\[x\\]\\(http://evil\\)" in body


@pytest.mark.parametrize(
    ("payload", "expected_unrenderable"),
    [
        ({}, 0),  # no 'assertions' key at all -- genuinely nothing was dropped
        ({"assertions": "not-a-list"}, 1),  # the whole field is malformed
        ({"assertions": ["not-a-mapping"]}, 1),
        # The malformed 'assertions' field is itself Mapping-shaped (Argus
        # round-3 P3 second finding): must still be treated as ONE
        # unrenderable CONTAINER-field error, never rendered as a legitimate
        # '### Row 1' -- a naive `isinstance(row, Mapping)` check on the
        # single-item `[raw_assertions]` fallback would wrongly accept it.
        ({"assertions": {"row_id": "x", "kind": "link"}}, 1),
    ],
    ids=["absent", "string", "list-of-non-mapping", "mapping"],
)
def test_graph_extraction_malformed_payload_renders_without_error(
    payload: dict[str, Any], expected_unrenderable: int
) -> None:
    artifact = render_proposal_diff(
        _proposal(
            lane="entity_resolution",
            kind="graph_extraction_candidate",
            proposal_id="sha256:extract-bad",
            payload=payload,
        )
    )
    assert "## Graph-extraction batch (per-document)" in artifact.pr_body
    assert artifact.pr_body.count("### Row ") == 0
    assert "- assertions: 0" in artifact.pr_body
    # The artifact must never UNDERSTATE what is stored -- a malformed
    # 'assertions' field is surfaced explicitly, not silently absorbed into
    # 'assertions: 0' with no trace (Argus round-2 P2).
    assert f"- unrenderable rows: {expected_unrenderable}" in artifact.pr_body


def test_graph_extraction_partial_malformed_batch_surfaces_unrenderable_count() -> None:
    """The concrete scenario the round-2 P2 finding names: a stored payload
    with 3 assertions of which 1 is a non-Mapping renders 'assertions: 2'
    with no indication anything was dropped under the round-1 renderer --
    understating what a steward would need to know is stored. The fix
    (`- unrenderable rows: N`) must make the drop explicit rather than
    invisible.
    """
    payload = {
        "document_ref": "doc:doc-9",
        "run_id": "run-1",
        "source_system": "sharepoint",
        "assertions": [
            {
                "row_id": "row-ok",
                "kind": "link",
                "entity_class": "Account",
                "entity_key": "k-acme",
                "confidence": 0.9,
                "evidence_ref": "chunk:doc-9:c3",
            },
            "not-a-mapping",
            42,
        ],
    }
    artifact = render_proposal_diff(
        _proposal(
            lane="entity_resolution",
            kind="graph_extraction_candidate",
            proposal_id="sha256:extract-partial",
            payload=payload,
        )
    )
    assert "- assertions: 1" in artifact.pr_body
    assert "- unrenderable rows: 2" in artifact.pr_body
    # patch is the review document's OWN unified diff (pr_body embeds it a
    # second time, nested inside the fenced diff block -- the
    # test_graph_extraction_batch_renders_per_row_blocks precedent).
    assert artifact.patch.count("### Row ") == 1


def test_graph_extraction_row_numbering_preserves_stored_indices() -> None:
    """Argus round-3 P3: a steward cross-referencing raw stored
    ``assertions[N-1]`` by heading number must land on the SAME assertion.
    Under the old renderer (enumerating only renderable rows starting at 1),
    a payload of [good, non-mapping, 42, good] rendered the second good row
    as ``### Row 2`` while it is stored at index 3 (0-based) / row 4 (1-based)
    -- a silent mismatch. The fix enumerates over the RAW rows and skips
    non-Mappings inside the loop, so a dropped row leaves a visible GAP in
    the numbering instead of shifting everything after it.
    """
    payload = {
        "document_ref": "doc:doc-9",
        "run_id": "run-1",
        "source_system": "sharepoint",
        "assertions": [
            {
                "row_id": "row-a",
                "kind": "link",
                "entity_class": "Account",
                "entity_key": "k-acme",
                "confidence": 0.9,
                "evidence_ref": "chunk:doc-9:c3",
            },
            "not-a-mapping",
            42,
            {
                "row_id": "row-d",
                "kind": "link",
                "entity_class": "Account",
                "entity_key": "k-globex",
                "confidence": 0.8,
                "evidence_ref": "chunk:doc-9:c4",
            },
        ],
    }
    artifact = render_proposal_diff(
        _proposal(
            lane="entity_resolution",
            kind="graph_extraction_candidate",
            proposal_id="sha256:extract-numbering",
            payload=payload,
        )
    )
    pr_body = artifact.pr_body
    assert "### Row 1" in pr_body
    assert "### Row 4" in pr_body
    assert "### Row 2" not in pr_body
    assert "### Row 3" not in pr_body
    assert "- assertions: 2" in pr_body
    assert "- unrenderable rows: 2" in pr_body
    assert pr_body.index("row-a") < pr_body.index("row-d")


def test_graph_extraction_artifact_carries_refs_never_chunk_text() -> None:
    # The canary stands in for verbatim source-chunk prose (the C12 fixture,
    # test_proposals.py precedent): the batch payload is built entirely from
    # TYPED distilled values, so rendering it surfaces the evidence REF with
    # click-through, never the chunk prose the ref points at. See
    # test_graph_extraction_renderer_surfaces_whatever_properties_carry for
    # the honest counterpart that gives this assertion teeth.
    canary = "CANARY the quarterly report says Acme acquired Globex"
    artifact = render_proposal_diff(_graph_extraction_proposal())
    assert canary not in artifact.pr_body
    assert canary not in artifact.patch
    assert "chunk:doc-9:c4" in artifact.pr_body


def test_graph_extraction_renderer_surfaces_whatever_properties_carry() -> None:
    """Honest counterpart: the renderer does not scrub property VALUES.

    Proves the test above is falsifiable, not vacuous: the SAME canary,
    routed through a ``properties`` value (a legitimate, schema-permitted
    channel this renderer faithfully displays), DOES appear in the rendered
    artifact. The renderer's job is to surface whatever typed business data
    the payload carries — it is not, and cannot be, a content-based leak
    scrubber. The real C12 guard is upstream: ``extra="forbid"`` blocks new
    fields at the schema, and disciplined population of ``properties`` with
    distilled values only is a NE-T2X-2 job-authoring convention.
    """
    canary = "CANARY the quarterly report says Acme acquired Globex"
    artifact = render_proposal_diff(_graph_extraction_proposal(properties={"excerpt": canary}))
    assert canary in artifact.pr_body
    assert "chunk:doc-9:c4" in artifact.patch


def test_graph_extraction_row_rendering_covers_every_schema_field() -> None:
    """Coverage ratchet (Argus round-2 P2): ``_graph_extraction_assertion_lines``
    hard-codes a field list rather than deriving it from the schema, so a
    field added to ``GraphExtractionAssertion`` later (e.g. an ACL hint) would
    render INVISIBLY with no test failure. That matters here specifically
    because the rendered artifact is the steward's ONLY view of the batch
    (the raw payload is deliberately kept off the API wire) — an unrendered
    but populated field is a silent review gap, not a cosmetic omission.

    This test builds a fully-populated row (every CURRENT schema field set to
    a unique marker value, via the real pydantic model so the fixture is
    itself schema-valid) and asserts every marker reaches the rendered
    output. It also PINS the field-name set so a schema addition fails this
    test directly, forcing the renderer to be updated (or the omission
    explicitly documented) in the same change.
    """
    assert set(GraphExtractionAssertion.model_fields) == {
        "row_id",
        "kind",
        "entity_class",
        "entity_key",
        "display",
        "properties",
        "subject_key",
        "edge_kind",
        "object_key",
        "confidence",
        "evidence_ref",
    }, "GraphExtractionAssertion's field set changed -- update the renderer coverage too"

    assertion = GraphExtractionAssertion(
        row_id="cov-row-id-marker",
        # NEW_EDGE is used so entity_class/entity_key/display AND the edge
        # triple can be populated simultaneously (the kind-shape validator
        # only REQUIRES fields per kind; it does not forbid the others).
        kind=GraphExtractionAssertionKind.NEW_EDGE,
        entity_class="cov-entity-class-marker",
        entity_key="cov-entity-key-marker",
        display="cov-display-marker",
        properties={"cov-prop-key-marker": "cov-prop-value-marker"},
        subject_key="cov-subject-key-marker",
        edge_kind="cov-edge-kind-marker",
        object_key="cov-object-key-marker",
        confidence=0.42,
        evidence_ref="cov-evidence-ref-marker",
    )
    row = assertion.model_dump(mode="json")
    rendered = "\n".join(diffs_module._graph_extraction_assertion_lines(1, row))

    assert "cov-row-id-marker" in rendered
    assert "new\\_edge" in rendered  # kind, through _safe's inline escaping
    assert "cov-entity-class-marker" in rendered
    assert "cov-entity-key-marker" in rendered
    assert "cov-display-marker" in rendered
    assert "cov-prop-key-marker" in rendered
    assert "cov-prop-value-marker" in rendered
    assert "cov-subject-key-marker" in rendered
    assert "cov-edge-kind-marker" in rendered
    assert "cov-object-key-marker" in rendered
    assert "0.42" in rendered
    assert "cov-evidence-ref-marker" in rendered
