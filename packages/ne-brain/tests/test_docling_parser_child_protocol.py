# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Production-shaped controls for the isolated Docling child exit protocol."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ne.brain.ingest.docling_parser import (
    _CHILD_EXIT_AVAILABILITY,
    _CHILD_EXIT_CAPACITY,
    _CHILD_EXIT_CONVERSION,
    _CHILD_EXIT_DEPENDENCY,
    _CHILD_EXIT_ENVIRONMENT,
    _CHILD_EXIT_INFERENCE,
    _CHILD_EXIT_INTERNAL,
    _CHILD_EXIT_OUTPUT,
    _CHILD_EXIT_POLICY,
    _CHILD_EXIT_TIMEOUT,
    _CHILD_EXIT_TRANSPORT,
)

_SCRIPT = Path(__file__).parents[1] / "parser_env" / "parse_doc.py"
_EXIT_ENVIRONMENT = 70
_EXIT_CONVERSION = 71
_EXIT_OUTPUT = 72
_EXIT_DEPENDENCY = 73
_EXIT_POLICY = 74
_EXIT_CAPACITY = 75
_EXIT_AVAILABILITY = 76
_EXIT_TIMEOUT = 77
_EXIT_INFERENCE = 78
_EXIT_INTERNAL = 79
_EXIT_TRANSPORT = 80


def test_platform_driver_and_child_script_share_the_exact_fixed_protocol() -> None:
    assert tuple(range(70, 81)) == (
        _CHILD_EXIT_ENVIRONMENT,
        _CHILD_EXIT_CONVERSION,
        _CHILD_EXIT_OUTPUT,
        _CHILD_EXIT_DEPENDENCY,
        _CHILD_EXIT_POLICY,
        _CHILD_EXIT_CAPACITY,
        _CHILD_EXIT_AVAILABILITY,
        _CHILD_EXIT_TIMEOUT,
        _CHILD_EXIT_INFERENCE,
        _CHILD_EXIT_INTERNAL,
        _CHILD_EXIT_TRANSPORT,
    )


def _write(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _fake_docling(root: Path) -> None:
    for package in (
        root / "docling",
        root / "docling" / "datamodel",
        root / "docling_core",
        root / "docling_core" / "types",
    ):
        _write(package / "__init__.py", "")

    _write(
        root / "huggingface_hub" / "__init__.py",
        "",
    )
    _write(
        root / "huggingface_hub" / "errors.py",
        """
class LocalEntryNotFoundError(Exception):
    pass
""",
    )
    _write(
        root / "pypdfium2.py",
        """
class _TextPage:
    def get_text_range(self):
        import os
        if os.environ.get("NE_TEST_DOCLING_SCENARIO") == "pdf-artifact-unavailable-empty":
            return ""
        return "Synthetic disclosure title\\r\\nSafe embedded PDF body"
    def close(self):
        pass

class _Page:
    def get_textpage(self):
        return _TextPage()
    def close(self):
        pass

class PdfDocument:
    def __init__(self, source):
        assert source
    def __len__(self):
        return 1
    def __getitem__(self, index):
        assert index == 0
        return _Page()
    def close(self):
        pass
""",
    )
    _write(
        root / "docling" / "datamodel" / "base_models.py",
        """
import os
if os.environ.get("NE_TEST_DOCLING_SCENARIO") == "import-failure":
    raise ImportError("IMPORT-DETAIL-MUST-NOT-LEAK")

class InputFormat:
    PDF = "pdf"
    DOCX = "docx"
    PPTX = "pptx"
    HTML = "html"
    MD = "md"

class DocumentStream:
    def __init__(self, *, name, stream):
        self.name = name
        self.stream = stream
""",
    )
    _write(
        root / "docling_core" / "types" / "doc.py",
        """
import os

class _Text:
    def __init__(self, text="safe parsed text"):
        self.text = text
        self.prov = []

class CodeItem(_Text):
    pass
class ListItem(_Text):
    pass
class PictureItem(_Text):
    pass
class SectionHeaderItem(_Text):
    pass
class TextItem(_Text):
    pass
class TitleItem(_Text):
    pass

class _UnencodableText:
    def __bool__(self):
        return True
    def strip(self):
        return "not empty"

class TableItem(_Text):
    def export_to_markdown(self, *, doc):
        del doc
        if os.environ.get("NE_TEST_DOCLING_SCENARIO") == "table-export-failure":
            raise RuntimeError("TABLE-DETAIL-MUST-NOT-LEAK")
        return _UnencodableText()
""",
    )
    _write(
        root / "docling" / "document_converter.py",
        """
import os
import errno
from docling_core.types.doc import TableItem, TextItem
from huggingface_hub.errors import LocalEntryNotFoundError

class _Value:
    def __init__(self, value):
        self.value = value

class _Error:
    def __init__(self, category):
        self.category = _Value(category)

class _Document:
    def __init__(self, item):
        self._item = item
    def iterate_items(self):
        scenario = os.environ.get("NE_TEST_DOCLING_SCENARIO")
        if scenario == "iterate-failure":
            raise RuntimeError("ITERATE-DETAIL-MUST-NOT-LEAK")
        if scenario == "malformed-iterate-item":
            return [self._item]
        return [(self._item, 0)]

class _Input:
    def __init__(self, valid):
        self.valid = valid

class _Result:
    def __init__(
        self,
        status,
        *,
        errors=(),
        item=None,
        input_valid=True,
        include_input=True,
        document_none=False,
    ):
        self.status = _Value(status)
        self.errors = list(errors)
        self.document = None if document_none else _Document(item or TextItem())
        if include_input:
            self.input = _Input(input_valid)

class DocumentConverter:
    def __init__(self, *, allowed_formats):
        assert allowed_formats == ["pdf", "docx", "pptx", "html", "md"]
    def convert(self, source, *, raises_on_error):
        if hasattr(source, "stream"):
            assert source.name in {"source.md", "source.pdf"}
        else:
            assert source.endswith(("source.md", "source.pdf"))
        assert raises_on_error is False
        scenario = os.environ.get("NE_TEST_DOCLING_SCENARIO", "success")
        if scenario == "air-gap" and os.environ.get("HF_HUB_OFFLINE") != "1":
            raise RuntimeError("AIR-GAP-OFFLINE-MARKER-MISSING")
        if scenario == "offline-always" and os.environ.get("HF_HUB_OFFLINE") != "1":
            raise RuntimeError("NON-AIR-GAP-NETWORK-WOULD-BE-ALLOWED")
        if scenario == "malformed":
            return _Result("failure", input_valid=False)
        if scenario == "backend-category":
            return _Result("failure", errors=[_Error("backend_failure")])
        if scenario == "empty-errors-valid":
            return _Result("failure", input_valid=True)
        if scenario == "empty-errors-missing-input":
            return _Result("failure", include_input=False)
        if scenario == "empty-errors-malformed-validity":
            return _Result("failure", input_valid="false")
        if scenario == "policy-or-runtime":
            return _Result("failure", errors=[_Error("unknown")])
        if scenario in {
            "policy", "capacity", "source_unavailable", "target_unavailable",
            "timeout", "inference_failure", "internal",
        }:
            return _Result("failure", errors=[_Error(scenario)])
        if scenario == "mixed-runtime-categories":
            return _Result("failure", errors=[_Error("timeout"), _Error("capacity")])
        if scenario in {"pdf-artifact-unavailable", "pdf-artifact-unavailable-empty"}:
            raise LocalEntryNotFoundError("MODEL-DETAIL-MUST-NOT-LEAK")
        if scenario == "pdf-artifact-cyclic-cause":
            error = LocalEntryNotFoundError("MODEL-DETAIL-MUST-NOT-LEAK")
            error.__cause__ = error
            raise error
        if scenario == "pdf-arbitrary-cyclic-cause":
            error = RuntimeError("RUNTIME-DETAIL-MUST-NOT-LEAK")
            error.__cause__ = error
            raise error
        if scenario == "pdf-converter-must-not-run":
            raise RuntimeError("CONVERTER-MUST-NOT-RUN")
        if scenario == "pdf-arbitrary-readonly-error":
            raise OSError(errno.EROFS, "ARBITRARY-DETAIL-MUST-NOT-LEAK")
        if scenario == "runtime-crash":
            raise RuntimeError("RUNTIME-DETAIL-MUST-NOT-LEAK")
        if scenario == "invalid-output":
            return _Result("success", item=TableItem())
        if scenario == "document-none":
            return _Result("success", document_none=True)
        if scenario in {"iterate-failure", "malformed-iterate-item"}:
            return _Result("success")
        if scenario == "table-export-failure":
            return _Result("success", item=TableItem())
        if scenario == "provenance-failure":
            item = TextItem()
            item.prov = [object()]
            return _Result("success", item=item)
        return _Result("success")
""",
    )


def _run_child(
    tmp_path: Path,
    scenario: str,
    *,
    air_gap: bool = False,
    missing_output_parent: bool = False,
    suffix: str = ".md",
    configured_artifacts: bool = True,
) -> subprocess.CompletedProcess[str]:
    fake_root = tmp_path / "fake-site"
    _fake_docling(fake_root)
    source = tmp_path / f"source{suffix}"
    source.write_bytes(b"%PDF synthetic\n" if suffix == ".pdf" else b"# supported markdown\n")
    output = (
        tmp_path / "missing" / "parsed.json" if missing_output_parent else tmp_path / "parsed.json"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(fake_root)
    env["NE_TEST_DOCLING_SCENARIO"] = scenario
    if suffix == ".pdf" and configured_artifacts:
        artifacts = tmp_path / "configured-docling-artifacts"
        artifacts.mkdir()
        env["DOCLING_ARTIFACTS_PATH"] = str(artifacts)
    elif suffix == ".pdf":
        env["HF_HOME"] = str(tmp_path / "empty-hf-home")
        env.pop("HF_HUB_CACHE", None)
    if air_gap:
        env["AIR_GAP"] = "true"
        env.pop("HF_HUB_OFFLINE", None)
    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--input",
            str(source),
            "--output",
            str(output),
            "--document-id",
            "doc-1",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=5,
    )
    if result.returncode == 0:
        parsed = json.loads(output.read_text(encoding="utf-8"))
        assert parsed["document_id"] == "doc-1"
        assert parsed["source_format"] == suffix.lstrip(".")
        expected_text = "Synthetic disclosure title" if suffix == ".pdf" else "safe parsed text"
        assert parsed["blocks"][0]["text"] == expected_text
    return result


@pytest.mark.parametrize(
    ("scenario", "air_gap"),
    [("success", False), ("air-gap", True), ("offline-always", False)],
)
def test_supported_document_success_uses_zero_exit_and_air_gap_is_offline_before_import(
    tmp_path: Path, scenario: str, air_gap: bool
) -> None:
    result = _run_child(tmp_path, scenario, air_gap=air_gap)

    assert result.returncode == 0
    assert result.stderr == ""


def test_supported_document_writes_through_inherited_output_fd(tmp_path: Path) -> None:
    fake_root = tmp_path / "fake-site"
    _fake_docling(fake_root)
    source = tmp_path / "source.md"
    source.write_text("# supported markdown\n", encoding="utf-8")
    output = tmp_path / "parsed.json"
    source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    output_fd = os.open(
        output,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(fake_root)
    env["NE_TEST_DOCLING_SCENARIO"] = "success"
    try:
        result = subprocess.run(
            [
                sys.executable,
                str(_SCRIPT),
                "--input-fd",
                str(source_fd),
                "--source-name",
                source.name,
                "--output-fd",
                str(output_fd),
                "--document-id",
                "doc-1",
            ],
            check=False,
            capture_output=True,
            text=True,
            env=env,
            pass_fds=(source_fd, output_fd),
        )
    finally:
        os.close(source_fd)
        os.close(output_fd)

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
    parsed = json.loads(output.read_text(encoding="utf-8"))
    assert parsed["document_id"] == "doc-1"
    assert parsed["blocks"][0]["text"] == "safe parsed text"


def test_invalid_supported_document_without_errors_uses_conversion_exit(tmp_path: Path) -> None:
    result = _run_child(tmp_path, "malformed")

    assert result.returncode == _EXIT_CONVERSION
    assert result.stdout == ""
    assert result.stderr == ""


def test_backend_failure_category_uses_conversion_exit(tmp_path: Path) -> None:
    result = _run_child(tmp_path, "backend-category")

    assert result.returncode == _EXIT_CONVERSION
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.parametrize(
    "scenario",
    [
        "empty-errors-valid",
        "empty-errors-missing-input",
        "empty-errors-malformed-validity",
    ],
)
def test_empty_errors_without_exact_invalid_input_use_internal_exit(
    tmp_path: Path, scenario: str
) -> None:
    result = _run_child(tmp_path, scenario)

    assert result.returncode == _EXIT_INTERNAL
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("scenario", "expected_exit"),
    [
        ("import-failure", _EXIT_DEPENDENCY),
        ("policy-or-runtime", _EXIT_INTERNAL),
        ("runtime-crash", _EXIT_INTERNAL),
        ("policy", _EXIT_POLICY),
        ("capacity", _EXIT_CAPACITY),
        ("source_unavailable", _EXIT_AVAILABILITY),
        ("target_unavailable", _EXIT_AVAILABILITY),
        ("timeout", _EXIT_TIMEOUT),
        ("inference_failure", _EXIT_INFERENCE),
        ("internal", _EXIT_INTERNAL),
        ("mixed-runtime-categories", _EXIT_INTERNAL),
    ],
)
def test_runtime_failures_use_content_free_enumerated_exit(
    tmp_path: Path, scenario: str, expected_exit: int
) -> None:
    result = _run_child(tmp_path, scenario)

    assert result.returncode == expected_exit
    assert "MUST-NOT-LEAK" not in result.stdout
    assert "MUST-NOT-LEAK" not in result.stderr


def test_contract_encoding_failure_uses_output_exit(tmp_path: Path) -> None:
    result = _run_child(tmp_path, "invalid-output")

    assert result.returncode == _EXIT_OUTPUT
    assert result.stdout == ""
    assert result.stderr == ""


@pytest.mark.parametrize(
    "scenario",
    [
        "document-none",
        "iterate-failure",
        "malformed-iterate-item",
        "table-export-failure",
        "provenance-failure",
    ],
)
def test_post_success_document_contract_failures_use_output_exit(
    tmp_path: Path, scenario: str
) -> None:
    result = _run_child(tmp_path, scenario)

    assert result.returncode == _EXIT_OUTPUT
    assert result.stdout == ""
    assert result.stderr == ""


def test_output_path_transport_failure_is_not_document_conversion(tmp_path: Path) -> None:
    result = _run_child(tmp_path, "success", missing_output_parent=True)

    assert result.returncode == _EXIT_TRANSPORT
    assert result.stdout == ""
    assert result.stderr == ""


def test_pdf_model_artifact_miss_falls_back_to_pinned_native_text_without_leak(
    tmp_path: Path,
) -> None:
    result = _run_child(tmp_path, "pdf-artifact-unavailable", suffix=".pdf", air_gap=True)

    assert result.returncode == 0
    assert "MUST-NOT-LEAK" not in result.stdout
    assert "MUST-NOT-LEAK" not in result.stderr


def test_pdf_model_artifact_cyclic_exception_chain_terminates_safely(tmp_path: Path) -> None:
    result = _run_child(tmp_path, "pdf-artifact-cyclic-cause", suffix=".pdf")

    assert result.returncode == 0
    assert "MUST-NOT-LEAK" not in result.stdout
    assert "MUST-NOT-LEAK" not in result.stderr


def test_arbitrary_pdf_cyclic_exception_chain_terminates_without_fallback(
    tmp_path: Path,
) -> None:
    result = _run_child(tmp_path, "pdf-arbitrary-cyclic-cause", suffix=".pdf")

    assert result.returncode == _EXIT_INTERNAL
    assert "MUST-NOT-LEAK" not in result.stdout
    assert "MUST-NOT-LEAK" not in result.stderr


def test_absent_pdf_artifact_cache_uses_native_text_before_converter(tmp_path: Path) -> None:
    result = _run_child(
        tmp_path,
        "pdf-converter-must-not-run",
        suffix=".pdf",
        configured_artifacts=False,
    )

    assert result.returncode == 0
    assert "CONVERTER-MUST-NOT-RUN" not in result.stdout
    assert "CONVERTER-MUST-NOT-RUN" not in result.stderr


def test_text_free_pdf_fallback_fails_as_document_conversion(tmp_path: Path) -> None:
    result = _run_child(tmp_path, "pdf-artifact-unavailable-empty", suffix=".pdf")

    assert result.returncode == _EXIT_CONVERSION
    assert "MUST-NOT-LEAK" not in result.stdout
    assert "MUST-NOT-LEAK" not in result.stderr


def test_arbitrary_pdf_readonly_error_does_not_trigger_native_fallback(tmp_path: Path) -> None:
    result = _run_child(tmp_path, "pdf-arbitrary-readonly-error", suffix=".pdf")

    assert result.returncode == _EXIT_INTERNAL
    assert "MUST-NOT-LEAK" not in result.stdout
    assert "MUST-NOT-LEAK" not in result.stderr
