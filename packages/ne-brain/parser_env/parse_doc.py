# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Docling codegen entrypoint — runs INSIDE the isolated hash-pinned parser venv.

This module imports `docling`, which is deliberately NOT part of the platform
virtualenv (its transitive closure pulls in torch and, on linux, proprietary
NVIDIA CUDA runtime wheels — SPEC §2 C4). It is executed as a subprocess by
``ne.brain.ingest.docling_parser`` against
``packages/ne-brain/.parser-venv``.

Given an input document it writes the ``ne.parsed_document/1`` JSON contract
to --output: a flat list of structural blocks (heading/paragraph/table/
list_item/code/caption/other) with 1-based page provenance where available.

Input formats are hard-allowlisted to PDF/DOCX/PPTX/HTML/MD — this is a
mitigation for CVE-2026-24009 (unsafe PyYAML parsing of YAML-format docs) and
CVE-2026-44018 (unsafe archive/XML in the METS-GBS backend): both attack
surfaces are structurally excluded here regardless of upstream fix status.
"""

# pyright: reportMissingImports=false, reportMissingModuleSource=false
from __future__ import annotations

import argparse
import errno
import json
import os
from io import BytesIO
from pathlib import Path
from typing import Any

os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ["HF_HUB_OFFLINE"] = "1"

# This content-processing subprocess never receives independent network
# authority. The connector's source access does not authorize Docling/Hugging
# Face egress, even when the wider deployment is not AIR_GAP. Force local-only
# model resolution before the first Docling import. A missing optional PDF
# layout model can use the bounded embedded-text fallback below; every other
# missing dependency fails closed through the enumerated child protocol.

# docling's classes are unresolvable from the platform pyright pass (docling
# lives only in the isolated .parser-venv, never the platform venv), so every
# docling-typed value in this module is deliberately `Any` rather than
# `object` — `object` would make isinstance-narrowed attribute access on these
# Unknown-imported classes fail pyright's `reportAttributeAccessIssue`.

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

_SUCCESS_STATUSES = frozenset({"success", "partial_success"})
_DOCUMENT_FAILURE_CATEGORIES = frozenset({"backend_failure"})
_RUNTIME_CATEGORY_EXIT = {
    "policy": _EXIT_POLICY,
    "capacity": _EXIT_CAPACITY,
    "source_unavailable": _EXIT_AVAILABILITY,
    "target_unavailable": _EXIT_AVAILABILITY,
    "timeout": _EXIT_TIMEOUT,
    "inference_failure": _EXIT_INFERENCE,
    "internal": _EXIT_INTERNAL,
    "unknown": _EXIT_INTERNAL,
}

_SUFFIX_TO_FORMAT = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".pptx": "pptx",
    ".html": "html",
    ".htm": "html",
    ".md": "md",
}


def _page_of(item: Any) -> int | None:
    prov = getattr(item, "prov", None)
    if not prov:
        return None
    return prov[0].page_no


class _DoclingBindings:
    """Lazy imports keep dependency failures inside the typed child protocol."""

    def __init__(self) -> None:
        from docling.datamodel.base_models import DocumentStream, InputFormat
        from docling.document_converter import DocumentConverter
        from docling_core.types.doc import (
            CodeItem,
            ListItem,
            PictureItem,
            SectionHeaderItem,
            TableItem,
            TextItem,
            TitleItem,
        )

        self.document_converter = DocumentConverter
        self.document_stream = DocumentStream
        self.allowed_formats = [
            InputFormat.PDF,
            InputFormat.DOCX,
            InputFormat.PPTX,
            InputFormat.HTML,
            InputFormat.MD,
        ]
        self.code_item = CodeItem
        self.list_item = ListItem
        self.picture_item = PictureItem
        self.section_header_item = SectionHeaderItem
        self.table_item = TableItem
        self.text_item = TextItem
        self.title_item = TitleItem


class _DocumentConversionFailure(RuntimeError):
    """The child positively identified a document/backend rejection."""


class _DocumentOutputFailure(RuntimeError):
    """A successful conversion produced an unusable parsed-document contract."""


class _ChildRuntimeFailure(RuntimeError):
    """Content-free child/runtime classification carried only by an exit code."""

    def __init__(self, exit_code: int) -> None:
        self.exit_code = exit_code


class _ChildTransportFailure(RuntimeError):
    """The child could not read or write its inherited transport."""


def _enum_value(value: Any) -> str | None:
    raw = getattr(value, "value", value)
    return raw if isinstance(raw, str) else None


def _model_artifact_unavailable(exc: BaseException) -> bool:
    """Identify only Hugging Face model-cache unavailability, without messages."""
    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        module = type(current).__module__
        name = type(current).__name__
        if module == "huggingface_hub.errors" and name in {
            "LocalEntryNotFoundError",
            "OfflineModeIsEnabled",
        }:
            return True
        current = current.__cause__ or current.__context__
    if not isinstance(exc, OSError) or exc.errno not in {errno.EACCES, errno.EROFS}:
        return False
    traceback = exc.__traceback__
    while traceback is not None:
        module = traceback.tb_frame.f_globals.get("__name__")
        if isinstance(module, str) and module.startswith("huggingface_hub"):
            return True
        traceback = traceback.tb_next
    return False


def _pdf_layout_artifacts_maybe_available() -> bool:
    configured = os.environ.get("DOCLING_ARTIFACTS_PATH")
    if configured:
        return Path(configured).is_dir()
    cache_root = Path(
        os.environ.get("HF_HUB_CACHE")
        or Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")) / "hub"
    )
    required_repositories = (
        "models--docling-project--docling-layout-heron",
        "models--docling-project--docling-models",
    )
    return all(
        any(path.is_dir() for path in (cache_root / repository / "snapshots").glob("*"))
        for repository in required_repositories
    )


def _read_fd_bytes(input_fd: int) -> bytes:
    try:
        os.lseek(input_fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while chunk := os.read(input_fd, 64 * 1024):
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError:
        raise _ChildTransportFailure from None


def _native_pdf_blocks(source: Any) -> list[dict[str, object]]:
    """Extract embedded PDF text when Docling's optional layout artifacts are absent."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        raise _ChildRuntimeFailure(_EXIT_DEPENDENCY) from None

    document: Any = None
    try:
        document = pdfium.PdfDocument(source)
        blocks: list[dict[str, object]] = []
        for page_index in range(len(document)):
            page = document[page_index]
            text_page: Any = None
            try:
                text_page = page.get_textpage()
                text = text_page.get_text_range()
            finally:
                if text_page is not None:
                    text_page.close()
                page.close()
            for line in text.splitlines():
                normalized = line.strip()
                if normalized:
                    blocks.append(
                        {
                            "kind": "paragraph",
                            "text": normalized,
                            "level": None,
                            "page": page_index + 1,
                        }
                    )
        if not blocks:
            raise _DocumentConversionFailure
        return blocks
    except _DocumentConversionFailure:
        raise
    except RuntimeError:
        raise _DocumentConversionFailure from None
    except Exception:
        raise _ChildRuntimeFailure(_EXIT_INTERNAL) from None
    finally:
        if document is not None:
            document.close()


def _block_from_item(item: Any, doc: Any, bindings: _DoclingBindings) -> dict[str, object] | None:
    text = getattr(item, "text", None)
    if isinstance(item, bindings.table_item):
        table_text = item.export_to_markdown(doc=doc)
        if not table_text or not table_text.strip():
            return None
        return {"kind": "table", "text": table_text, "level": None, "page": _page_of(item)}
    if isinstance(item, bindings.title_item):
        if not text or not text.strip():
            return None
        return {"kind": "heading", "text": text, "level": 1, "page": _page_of(item)}
    if isinstance(item, bindings.section_header_item):
        if not text or not text.strip():
            return None
        level = getattr(item, "level", 1) or 1
        return {"kind": "heading", "text": text, "level": level, "page": _page_of(item)}
    if isinstance(item, bindings.list_item):
        if not text or not text.strip():
            return None
        return {"kind": "list_item", "text": text, "level": None, "page": _page_of(item)}
    if isinstance(item, bindings.code_item):
        if not text or not text.strip():
            return None
        return {"kind": "code", "text": text, "level": None, "page": _page_of(item)}
    if isinstance(item, bindings.picture_item):
        captions = getattr(item, "captions", None) or []
        parts: list[str] = []
        for cap_ref in captions:
            resolve_fn = getattr(cap_ref, "resolve", None)
            resolved = resolve_fn(doc) if callable(resolve_fn) else cap_ref
            cap_text = getattr(resolved, "text", None)
            if cap_text:
                parts.append(cap_text)
        caption_text = " ".join(parts).strip()
        if not caption_text:
            return None
        return {"kind": "caption", "text": caption_text, "level": None, "page": _page_of(item)}
    if isinstance(item, bindings.text_item):
        if not text or not text.strip():
            return None
        return {"kind": "paragraph", "text": text, "level": None, "page": _page_of(item)}
    if text and text.strip():
        return {"kind": "other", "text": text, "level": None, "page": _page_of(item)}
    return None


def parse(
    input_path: Path,
    document_id: str,
    *,
    input_fd: int | None = None,
    source_name: str | None = None,
) -> dict[str, object]:
    source_path = Path(source_name) if source_name is not None else input_path
    source: Any = str(input_path)
    source_bytes: bytes | None = None
    if input_fd is not None:
        source_bytes = _read_fd_bytes(input_fd)
        source = BytesIO(source_bytes)
    try:
        bindings = _DoclingBindings()
    except ImportError:
        raise _ChildRuntimeFailure(_EXIT_DEPENDENCY) from None
    if source_path.suffix.lower() == ".pdf" and not _pdf_layout_artifacts_maybe_available():
        if input_fd is None:
            native_source: Any = str(input_path)
        else:
            assert source_bytes is not None
            native_source = BytesIO(source_bytes)
        blocks = _native_pdf_blocks(native_source)
        return {
            "schema_version": "ne.parsed_document/1",
            "document_id": document_id,
            "source_name": source_path.name,
            "source_format": "pdf",
            "blocks": blocks,
            "metadata": {},
        }
    converter = bindings.document_converter(allowed_formats=bindings.allowed_formats)
    if input_fd is not None:
        source = bindings.document_stream(
            name=source_path.name,
            stream=source,
        )
    try:
        result = converter.convert(source, raises_on_error=False)
    except Exception as exc:
        if source_path.suffix.lower() != ".pdf" or not _model_artifact_unavailable(exc):
            raise _ChildRuntimeFailure(_EXIT_INTERNAL) from None
        if input_fd is None:
            native_source = str(input_path)
        else:
            assert source_bytes is not None
            native_source = BytesIO(source_bytes)
        blocks = _native_pdf_blocks(native_source)
        return {
            "schema_version": "ne.parsed_document/1",
            "document_id": document_id,
            "source_name": source_path.name,
            "source_format": "pdf",
            "blocks": blocks,
            "metadata": {},
        }
    status = _enum_value(getattr(result, "status", None))
    if status not in _SUCCESS_STATUSES:
        input_valid = getattr(getattr(result, "input", None), "valid", None)
        if input_valid is False:
            raise _DocumentConversionFailure
        categories = tuple(
            _enum_value(getattr(error, "category", None)) for error in getattr(result, "errors", ())
        )
        if categories and all(category in _DOCUMENT_FAILURE_CATEGORIES for category in categories):
            raise _DocumentConversionFailure
        runtime_codes = {
            _RUNTIME_CATEGORY_EXIT.get(category or "", _EXIT_INTERNAL) for category in categories
        }
        if len(runtime_codes) == 1:
            raise _ChildRuntimeFailure(runtime_codes.pop())
        raise _ChildRuntimeFailure(_EXIT_INTERNAL)
    try:
        doc = result.document

        blocks: list[dict[str, object]] = []
        for item, _level in doc.iterate_items():
            block = _block_from_item(item, doc, bindings)
            if block is not None:
                blocks.append(block)

        source_format = _SUFFIX_TO_FORMAT.get(
            source_path.suffix.lower(), source_path.suffix.lstrip(".")
        )
        return {
            "schema_version": "ne.parsed_document/1",
            "document_id": document_id,
            "source_name": source_path.name,
            "source_format": source_format,
            "blocks": blocks,
            "metadata": {},
        }
    except Exception:
        raise _DocumentOutputFailure from None


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse a document with docling.")
    input_source = parser.add_mutually_exclusive_group(required=True)
    input_source.add_argument("--input", type=Path)
    input_source.add_argument("--input-fd", type=int)
    parser.add_argument("--source-name")
    output = parser.add_mutually_exclusive_group(required=True)
    output.add_argument("--output", type=Path)
    output.add_argument("--output-fd", type=int)
    parser.add_argument("--document-id", required=True)
    args = parser.parse_args()

    try:
        if args.input_fd is not None:
            if not args.source_name:
                return _EXIT_ENVIRONMENT
            parsed = parse(
                Path(args.source_name),
                args.document_id,
                input_fd=args.input_fd,
                source_name=args.source_name,
            )
        else:
            parsed = parse(args.input, args.document_id)
    except _DocumentConversionFailure:
        return _EXIT_CONVERSION
    except _DocumentOutputFailure:
        return _EXIT_OUTPUT
    except _ChildRuntimeFailure as exc:
        return exc.exit_code
    except _ChildTransportFailure:
        return _EXIT_TRANSPORT
    except Exception:
        return _EXIT_INTERNAL

    try:
        encoded = json.dumps(parsed, indent=2)
    except (TypeError, ValueError, RecursionError):
        return _EXIT_OUTPUT
    except Exception:
        return _EXIT_INTERNAL
    try:
        if args.output_fd is not None:
            remaining = memoryview(encoded.encode("utf-8"))
            while remaining:
                written = os.write(args.output_fd, remaining)
                if written <= 0:
                    return _EXIT_TRANSPORT
                remaining = remaining[written:]
            os.fsync(args.output_fd)
        else:
            args.output.write_text(encoded, encoding="utf-8")
    except Exception:
        return _EXIT_TRANSPORT
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
