# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Bounded M365 file bytes -> existing Docling ``DocumentParser`` contract.

This module performs no egress. :mod:`ne.connect.m365.graph` supplies already
bounded bytes from the guarded canonical Graph content endpoint; this adapter
writes them to an ephemeral, suffix-correct path and invokes the existing
``ne-brain`` Docling parse seam. Only the parsed textual/structural result is
returned. The temporary binary is removed before the connector emits a Record.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import stat
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path, PurePath
from typing import Any, Literal, NamedTuple

from ne.brain.ingest.contracts import (
    DocumentParser,
    FileDescriptorDocumentParser,
    ParsedDocument,
    ParserConversionError,
    ParserEnvironmentError,
    ParserOutputError,
    ParserResidueError,
)
from ne.brain.parsed_evidence import ParsedDocumentEvidenceDescriptor
from ne.connect.endpoints import AirGapViolationError
from ne.connect.errors import GraphDocumentConversionError, GraphDocumentOutputError
from ne.connect.m365.contract import M365_BODY_PARSER_IDENTITY

__all__ = ["M365BodyParser", "supported_format"]

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

# Docling's own allowlist is suffix-based. Requiring the Graph MIME and file
# suffix to agree prevents a source from selecting a different parser backend by
# lying in only one metadata field.
_SUPPORTED: dict[tuple[str, str], str] = {
    (".docx", _DOCX_MIME): "docx",
    (".pdf", "application/pdf"): "pdf",
    (".pptx", _PPTX_MIME): "pptx",
    (".html", "text/html"): "html",
    (".htm", "text/html"): "html",
    (".md", "text/markdown"): "md",
    (".md", "text/plain"): "md",
}


class _TemporaryRoot(NamedTuple):
    path: Path
    root_fd: int
    parent_fd: int
    device: int
    inode: int
    owner: int


_ROOT_OPEN_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_OPEN_FLAGS = os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


def _capture_temporary_root(path: Path) -> _TemporaryRoot:
    """Open and bind an owner-only real directory before any document bytes land."""
    with contextlib.ExitStack() as close_on_error:
        parent_fd = os.open(path.parent, _ROOT_OPEN_FLAGS)
        close_on_error.callback(os.close, parent_fd)
        root_fd = os.open(path.name, _ROOT_OPEN_FLAGS, dir_fd=parent_fd)
        close_on_error.callback(os.close, root_fd)
        path_stat = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        fd_stat = os.fstat(root_fd)
        identity = (fd_stat.st_dev, fd_stat.st_ino, fd_stat.st_uid)
        if (
            not stat.S_ISDIR(path_stat.st_mode)
            or not stat.S_ISDIR(fd_stat.st_mode)
            or stat.S_IMODE(fd_stat.st_mode) != 0o700
            or fd_stat.st_uid != os.geteuid()
            or identity != (path_stat.st_dev, path_stat.st_ino, path_stat.st_uid)
        ):
            raise OSError("unsafe M365 temporary root")
        close_on_error.pop_all()
        return _TemporaryRoot(path, root_fd, parent_fd, *identity)


def _open_new_file_at(root: _TemporaryRoot, name: str) -> int:
    """Create one private regular file through the retained root descriptor."""
    if _temporary_root_state(root) != "same":
        raise ParserResidueError("M365 temporary root identity changed")
    file_fd: int | None = None
    try:
        file_fd = os.open(name, _FILE_OPEN_FLAGS, 0o600, dir_fd=root.root_fd)
        opened = os.fstat(file_fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_uid != root.owner
            or opened.st_dev != root.device
            or _temporary_root_state(root) != "same"
        ):
            raise ParserResidueError("M365 temporary root identity changed")
        return file_fd
    except Exception:
        if file_fd is not None:
            with contextlib.suppress(OSError):
                os.close(file_fd)
        raise


def _write_all_and_sync(file_fd: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        written = os.write(file_fd, remaining)
        if written <= 0:
            raise OSError("M365 staging write made no progress")
        remaining = remaining[written:]
    os.fsync(file_fd)


def _temporary_root_state(root: _TemporaryRoot) -> Literal["same", "missing", "changed"]:
    expected = (root.device, root.inode, root.owner)
    try:
        current = os.stat(root.path.name, dir_fd=root.parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        try:
            retained = os.fstat(root.root_fd)
        except OSError:
            return "changed"
        if (
            stat.S_ISDIR(retained.st_mode)
            and stat.S_IMODE(retained.st_mode) == 0o700
            and (retained.st_dev, retained.st_ino, retained.st_uid) == expected
        ):
            return "missing"
        return "changed"
    except OSError:
        return "changed"
    try:
        retained = os.fstat(root.root_fd)
    except OSError:
        return "changed"
    if (
        stat.S_ISDIR(current.st_mode)
        and stat.S_ISDIR(retained.st_mode)
        and stat.S_IMODE(current.st_mode) == 0o700
        and stat.S_IMODE(retained.st_mode) == 0o700
        and (current.st_dev, current.st_ino, current.st_uid) == expected
        and (retained.st_dev, retained.st_ino, retained.st_uid) == expected
    ):
        return "same"
    return "changed"


def _same_child(current: os.stat_result, expected: os.stat_result) -> bool:
    return (
        current.st_dev,
        current.st_ino,
        current.st_uid,
        stat.S_IFMT(current.st_mode),
    ) == (
        expected.st_dev,
        expected.st_ino,
        expected.st_uid,
        stat.S_IFMT(expected.st_mode),
    )


def _clean_directory_fd(
    root: _TemporaryRoot,
    directory_fd: int,
    *,
    expected_root_state: Literal["same", "missing"],
) -> tuple[bool, bool]:
    """Remove one anchored directory tree without following any path symlink."""
    had_issue = False
    if _temporary_root_state(root) != expected_root_state:
        return False, True
    if directory_fd != root.root_fd:
        with contextlib.suppress(OSError):
            os.fchmod(directory_fd, 0o700)
    try:
        names = os.listdir(directory_fd)
    except OSError:
        return False, True
    for name in names:
        if _temporary_root_state(root) != expected_root_state:
            return False, True
        try:
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError:
            had_issue = True
            continue
        if before.st_uid != root.owner:
            had_issue = True
            continue
        if stat.S_ISDIR(before.st_mode):
            if before.st_dev != root.device:
                had_issue = True
                continue
            child_fd: int | None = None
            try:
                child_fd = os.open(name, _ROOT_OPEN_FLAGS, dir_fd=directory_fd)
                if not _same_child(os.fstat(child_fd), before):
                    had_issue = True
                    continue
                child_clean, child_issue = _clean_directory_fd(
                    root,
                    child_fd,
                    expected_root_state=expected_root_state,
                )
                had_issue |= child_issue
            except OSError:
                had_issue = True
                child_clean = False
            finally:
                if child_fd is not None:
                    os.close(child_fd)
            if child_clean and _temporary_root_state(root) == expected_root_state:
                try:
                    current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                    if (
                        _same_child(current, before)
                        and _temporary_root_state(root) == expected_root_state
                    ):
                        os.rmdir(name, dir_fd=directory_fd)
                    else:
                        had_issue = True
                except FileNotFoundError:
                    pass
                except OSError:
                    had_issue = True
        else:
            try:
                current = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
                if (
                    _same_child(current, before)
                    and _temporary_root_state(root) == expected_root_state
                ):
                    os.unlink(name, dir_fd=directory_fd)
                else:
                    had_issue = True
            except FileNotFoundError:
                pass
            except OSError:
                had_issue = True
    if _temporary_root_state(root) != expected_root_state:
        return False, True
    try:
        return not os.listdir(directory_fd), had_issue
    except OSError:
        return False, True


def _cleanup_temporary_directory(root: _TemporaryRoot) -> tuple[bool, bool]:
    """Bounded fd-anchored cleanup returning ``(had_issue, residue_remains)``."""
    had_issue = False
    try:
        initial_state = _temporary_root_state(root)
        if initial_state == "changed":
            return True, True
        for _attempt in range(2):
            clean, attempt_issue = _clean_directory_fd(
                root,
                root.root_fd,
                expected_root_state=initial_state,
            )
            had_issue |= attempt_issue
            state = _temporary_root_state(root)
            if state != initial_state:
                return True, True
            if initial_state == "missing":
                if clean:
                    return True, True
                continue
            if clean:
                try:
                    current = os.stat(root.path.name, dir_fd=root.parent_fd, follow_symlinks=False)
                    if stat.S_ISDIR(current.st_mode) and (
                        current.st_dev,
                        current.st_ino,
                        current.st_uid,
                    ) == (root.device, root.inode, root.owner):
                        os.rmdir(root.path.name, dir_fd=root.parent_fd)
                    else:
                        return True, True
                except FileNotFoundError:
                    return True, True
                except OSError:
                    had_issue = True
                else:
                    return had_issue, False
        return True, True
    finally:
        with contextlib.suppress(OSError):
            os.close(root.root_fd)
        with contextlib.suppress(OSError):
            os.close(root.parent_fd)


def supported_format(item: Mapping[str, Any]) -> str | None:
    """Return the agreed Docling format, or ``None`` for honest metadata-only."""
    file_facet = item.get("file")
    if not isinstance(file_facet, Mapping):
        return None
    mime = file_facet.get("mimeType")
    name = item.get("name")
    if not isinstance(mime, str) or not isinstance(name, str):
        return None
    normalized_mime = mime.lower().split(";", 1)[0].strip()
    return _SUPPORTED.get((PurePath(name).suffix.lower(), normalized_mime))


class M365BodyParser:
    """Adapt bounded downloaded bytes to a validated textual Record payload."""

    def __init__(
        self,
        parser: DocumentParser | None = None,
    ) -> None:
        self._parser = parser
        self.__evidence_sink: (
            Callable[[str, str, ParsedDocumentEvidenceDescriptor], None] | None
        ) = None
        self.__evidence_issue: Callable[..., ParsedDocumentEvidenceDescriptor] | None = None

    def require_parser(self) -> FileDescriptorDocumentParser:
        """Resolve the optional Docling parser only for a supported generic body.

        Operational ``.negraph.json`` envelopes are parsed by their strict
        connector-owned schema and never need the heavyweight sidecar parser.
        Keeping this resolution lazy lets an operational-only deployment run
        from the installed application image, which intentionally carries no
        monorepo ``uv.lock`` or runtime parser environment. Generic supported
        documents still resolve before their content download and fail closed
        exactly as before when that parser environment is unavailable.
        """
        if self._parser is None:
            from ne.brain.ingest.docling_parser import DoclingParser

            self._parser = DoclingParser()
        if not isinstance(self._parser, FileDescriptorDocumentParser):
            raise ParserEnvironmentError("M365 parser fd input unavailable")
        return self._parser

    def _bind_evidence(
        self,
        *,
        evidence_sink: Callable[[str, str, ParsedDocumentEvidenceDescriptor], None],
        evidence_issue: Callable[..., ParsedDocumentEvidenceDescriptor],
    ) -> None:
        """Complete the one-time connector-owned evidence composition."""
        if self.__evidence_sink is not None or self.__evidence_issue is not None:
            raise TypeError("M365 parser evidence authority is already bound")
        self.__evidence_sink = evidence_sink
        self.__evidence_issue = evidence_issue

    async def parse(
        self,
        content: bytes,
        *,
        document_id: str,
        source_format: str,
        record_id: str | None = None,
        acl: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        parser = self.require_parser()
        suffix = f".{source_format}"
        temporary: _TemporaryRoot | None = None
        file_fd: int | None = None
        try:
            try:
                temporary_path = Path(tempfile.mkdtemp(prefix="ne-m365-body-"))
                try:
                    temporary = _capture_temporary_root(temporary_path)
                except OSError:
                    raise ParserResidueError("M365 temporary root identity invalid") from None
                path = temporary.path / f"source{suffix}"
                file_fd = _open_new_file_at(temporary, path.name)
                _write_all_and_sync(file_fd, content)
                if _temporary_root_state(temporary) != "same":
                    raise ParserResidueError("M365 temporary root identity changed")
            except OSError:
                raise ParserEnvironmentError("M365 document staging failed") from None

            try:
                parsed = await parser.parse_fd(
                    file_fd,
                    document_id=document_id,
                    source_name=path.name,
                )
                if _temporary_root_state(temporary) != "same":
                    raise ParserResidueError("M365 temporary root identity changed")
            except (ParserEnvironmentError, AirGapViolationError):
                raise
            except ParserOutputError:
                raise GraphDocumentOutputError(
                    "M365 parser returned invalid document output"
                ) from None
            except ParserConversionError:
                raise GraphDocumentConversionError("M365 document conversion failed") from None
            except Exception:
                raise GraphDocumentConversionError("M365 document conversion failed") from None
            self._validate(parsed, document_id=document_id, source_format=source_format)
        finally:
            if file_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(file_fd)
            if temporary is not None:
                active_error = sys.exc_info()[1]
                cleanup_issue, residue_remains = _cleanup_temporary_directory(temporary)
                if cleanup_issue:
                    if residue_remains:
                        raise ParserResidueError(
                            "M365 temporary document cleanup incomplete"
                        ) from None
                    if active_error is None:
                        raise ParserEnvironmentError(
                            "M365 document staging cleanup failed"
                        ) from None
                    active_error.add_note(
                        "M365 document staging cleanup incomplete"
                        if residue_remains
                        else "M365 document staging cleanup recovered after an error"
                    )
        payload = {
            "body": "\n\n".join(
                block.text.strip() for block in parsed.blocks if block.text.strip()
            ),
            "body_blocks": [block.model_dump(mode="json") for block in parsed.blocks],
            "body_block_count": len(parsed.blocks),
            "body_content_sha256": hashlib.sha256(content).hexdigest(),
            "body_ingestion_status": "parsed",
            "body_schema_version": parsed.schema_version,
            "body_source_format": parsed.source_format,
            "body_parser_identity": M365_BODY_PARSER_IDENTITY,
        }
        if self.__evidence_sink is not None or self.__evidence_issue is not None:
            if record_id is None or self.__evidence_sink is None or self.__evidence_issue is None:
                raise GraphDocumentOutputError("M365 parser evidence record identity unavailable")
            self.__evidence_sink(
                document_id,
                record_id,
                self.__evidence_issue(
                    source_id=document_id,
                    record_id=record_id,
                    acl=acl,
                    payload=payload,
                ),
            )
        return payload

    @staticmethod
    def _validate(parsed: ParsedDocument, *, document_id: str, source_format: str) -> None:
        if parsed.document_id != document_id:
            raise GraphDocumentOutputError("M365 parser returned a mismatched document identity")
        if parsed.source_format != source_format:
            raise GraphDocumentOutputError("M365 parser returned a mismatched source format")
        if not parsed.blocks or all(not block.text.strip() for block in parsed.blocks):
            raise GraphDocumentOutputError("M365 parser returned an empty document body")
