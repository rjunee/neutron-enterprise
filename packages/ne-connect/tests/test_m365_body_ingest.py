# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""BD-4 file-body fail-closed proof over the real sync/write seam."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
import tempfile
import threading
from pathlib import Path
from typing import Any, Never

import httpx
import pytest
import sqlalchemy as sa

import ne.brain.ingest.docling_parser as docling_parser_module
import ne.connect.m365.content as m365_content_module
from ne.audit.store import ne_audit_log
from ne.brain.ingest.contracts import (
    BlockKind,
    DocBlock,
    ParsedDocument,
    ParserConversionError,
    ParserEnvironmentError,
    ParserOutputError,
    ParserResidueError,
)
from ne.brain.ingest.docling_parser import DoclingParser, _venv_python_at
from ne.connect import ConnectorStateStore
from ne.connect.endpoints import AirGapViolationError
from ne.connect.errors import (
    GraphContentError,
    GraphDocumentConversionError,
    GraphDocumentOutputError,
    GraphDocumentParseError,
    GraphForbiddenError,
    GraphParseError,
    GraphRedirectError,
)
from ne.connect.m365 import run_m365_sync
from ne.connect.m365.connector import M365GraphConnector
from ne.connect.m365.content import M365BodyParser

from .conftest import WriterStack
from .m365_transport import RecordedBodyParser, build_m365_transport, m365_settings

_SOURCE = "m365-sharepoint"


class _FailingBodyParser:
    def __init__(self, error: Exception) -> None:
        self._error = error

    async def parse(self, path: Path, *, document_id: str) -> ParsedDocument:
        del path, document_id
        raise self._error

    async def parse_fd(self, file_fd: int, *, document_id: str, source_name: str) -> ParsedDocument:
        del file_fd, document_id, source_name
        raise self._error


class _OutputFailingBodyParser:
    def __init__(self, sentinel: str) -> None:
        self._sentinel = sentinel

    async def parse(self, path: Path, *, document_id: str) -> ParsedDocument:
        del path, document_id
        raise ParserOutputError(self._sentinel)

    async def parse_fd(self, file_fd: int, *, document_id: str, source_name: str) -> ParsedDocument:
        del file_fd, document_id, source_name
        raise ParserOutputError(self._sentinel)


class _ContractBodyParser:
    def __init__(self, violation: str) -> None:
        self._violation = violation

    async def parse(self, path: Path, *, document_id: str) -> ParsedDocument:
        return self._parsed(document_id=document_id, source_name=path.name)

    async def parse_fd(self, file_fd: int, *, document_id: str, source_name: str) -> ParsedDocument:
        del file_fd
        return self._parsed(document_id=document_id, source_name=source_name)

    def _parsed(self, *, document_id: str, source_name: str) -> ParsedDocument:
        returned_id = "wrong-document" if self._violation == "identity" else document_id
        source_format = (
            "pdf" if self._violation == "format" else Path(source_name).suffix.lstrip(".")
        )
        blocks = (
            ()
            if self._violation == "empty"
            else (DocBlock(kind=BlockKind.PARAGRAPH, text="safe fixture text"),)
        )
        return ParsedDocument(
            document_id=returned_id,
            source_name=Path(source_name).name,
            source_format=source_format,
            blocks=blocks,
        )


def _use_stage(stage: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stage.mkdir(mode=0o700)
    monkeypatch.setattr(tempfile, "mkdtemp", lambda **_kwargs: str(stage))


def _fail_first_unlink(monkeypatch: pytest.MonkeyPatch) -> None:
    original_unlink = os.unlink
    calls = 0

    def _unlink(path: str | bytes, *, dir_fd: int | None = None) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("UNLINK-PATH-MUST-NOT-LEAK")
        if dir_fd is None:
            original_unlink(path)
        else:
            original_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", _unlink)


def _block_all_deletion(monkeypatch: pytest.MonkeyPatch) -> None:
    def _refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("DELETE-PATH-MUST-NOT-LEAK")

    monkeypatch.setattr(os, "unlink", _refuse)
    monkeypatch.setattr(os, "rmdir", _refuse)


def _item_id(source_id: str, record_id: str) -> str:
    return hashlib.sha256(f"{_SOURCE}\n{source_id}\n{record_id}".encode()).hexdigest()


async def _assert_not_written(stack: WriterStack) -> None:
    assert await stack.store.get([_item_id("drive-1:item-user", "item-user")]) == []


def _audit_rows(stack: WriterStack) -> list[dict[str, object]]:
    with stack.engine.begin() as conn:
        return [
            dict(row)
            for row in conn.execute(sa.select(ne_audit_log).order_by(ne_audit_log.c.id)).mappings()
        ]


def _sync_events(stack: WriterStack) -> list[dict[str, object]]:
    return [row for row in _audit_rows(stack) if row["action"] == "connector.sync"]


def test_parser_failure_aborts_without_metadata_only_write_or_delta_advance(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    state = ConnectorStateStore()

    async def scenario() -> None:
        with pytest.raises(GraphDocumentConversionError) as excinfo:
            await run_m365_sync(
                m365_settings(),
                writer=writer_stack.writer,
                state_store=state,
                audit_sink=writer_stack.sink,
                actor="connector:test",
                transport=build_m365_transport(),
                parser=RecordedBodyParser(fail=True),
            )
        message = str(excinfo.value)
        assert type(excinfo.value) is GraphDocumentConversionError
        assert isinstance(excinfo.value, GraphDocumentParseError)
        assert isinstance(excinfo.value, GraphParseError)
        assert message == "M365 document conversion failed"
        assert "tempauth" not in message
        assert "DO-NOT-LEAK" not in message
        assert state.load_state(_SOURCE) == {}
        await _assert_not_written(writer_stack)
        sync_events = _sync_events(writer_stack)
        assert len(sync_events) == 1
        assert sync_events[0]["config_hash"] == frozen_config
        payload = sync_events[0]["payload"]
        assert isinstance(payload, dict)
        assert payload["detail"] == ("mode=backfill error_class=GraphDocumentConversionError")
        persisted = json.dumps(_audit_rows(writer_stack), default=str)
        assert "tempauth" not in persisted
        assert "DO-NOT-LEAK" not in persisted

    asyncio.run(scenario())


def test_generic_parser_failure_maps_directly_to_closed_conversion_category() -> None:
    sentinel = "PARSER-CONVERSION-DETAIL-MUST-NOT-LEAK"

    async def scenario() -> None:
        with pytest.raises(GraphDocumentConversionError) as excinfo:
            await M365BodyParser(_FailingBodyParser(RuntimeError(sentinel))).parse(
                b"fixture",
                document_id="doc-1",
                source_format="docx",
            )
        assert type(excinfo.value) is GraphDocumentConversionError
        assert isinstance(excinfo.value, GraphDocumentParseError)
        assert str(excinfo.value) == "M365 document conversion failed"
        assert sentinel not in str(excinfo.value)

    asyncio.run(scenario())


@pytest.mark.parametrize("violation", ["identity", "format", "empty"])
def test_post_parse_validation_maps_directly_to_closed_contract_category(
    violation: str,
) -> None:
    async def scenario() -> None:
        with pytest.raises(GraphDocumentOutputError) as excinfo:
            await M365BodyParser(_ContractBodyParser(violation)).parse(
                b"fixture",
                document_id="doc-1",
                source_format="docx",
            )
        assert type(excinfo.value) is GraphDocumentOutputError
        assert isinstance(excinfo.value, GraphDocumentParseError)
        assert "doc-1" not in str(excinfo.value)

    asyncio.run(scenario())


def test_invalid_parser_output_aborts_without_write_and_audits_contract_category(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    state = ConnectorStateStore()
    sentinel = "INVALID-PARSER-OUTPUT-MUST-NOT-LEAK"

    async def scenario() -> None:
        with pytest.raises(GraphDocumentOutputError) as excinfo:
            await run_m365_sync(
                m365_settings(),
                writer=writer_stack.writer,
                state_store=state,
                audit_sink=writer_stack.sink,
                actor="connector:test",
                transport=build_m365_transport(),
                parser=_OutputFailingBodyParser(sentinel),
            )
        assert type(excinfo.value) is GraphDocumentOutputError
        assert isinstance(excinfo.value, GraphDocumentParseError)
        assert str(excinfo.value) == "M365 parser returned invalid document output"
        assert state.load_state(_SOURCE) == {}
        await _assert_not_written(writer_stack)
        sync_events = _sync_events(writer_stack)
        assert len(sync_events) == 1
        assert sync_events[0]["config_hash"] == frozen_config
        payload = sync_events[0]["payload"]
        assert isinstance(payload, dict)
        assert payload["detail"] == ("mode=backfill error_class=GraphDocumentOutputError")
        assert sentinel not in json.dumps(_audit_rows(writer_stack), default=str)

    asyncio.run(scenario())


def test_temporary_directory_failure_is_typed_as_parser_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = "TEMPORARY-DIRECTORY-PATH-MUST-NOT-LEAK"

    def _fail_tempdir(*_args: object, **_kwargs: object) -> Never:
        raise OSError(sentinel)

    monkeypatch.setattr(tempfile, "mkdtemp", _fail_tempdir)

    async def scenario() -> None:
        with pytest.raises(ParserEnvironmentError) as excinfo:
            await M365BodyParser(RecordedBodyParser()).parse(
                b"fixture",
                document_id="doc-1",
                source_format="docx",
            )
        assert type(excinfo.value) is ParserEnvironmentError
        assert str(excinfo.value) == "M365 document staging failed"
        assert sentinel not in str(excinfo.value)

    asyncio.run(scenario())


def test_non_owner_only_staging_root_is_refused_before_bytes_land(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    stage.mkdir(mode=0o755)
    stage.chmod(0o755)
    monkeypatch.setattr(tempfile, "mkdtemp", lambda **_kwargs: str(stage))

    async def scenario() -> None:
        with pytest.raises(ParserResidueError) as excinfo:
            await M365BodyParser(RecordedBodyParser()).parse(
                b"CUSTOMER-BYTES-MUST-NOT-LAND",
                document_id="doc-1",
                source_format="docx",
            )
        assert type(excinfo.value) is ParserResidueError
        assert str(excinfo.value) == "M365 temporary root identity invalid"
        assert list(stage.iterdir()) == []

    asyncio.run(scenario())


@pytest.mark.parametrize("drifted_mode", [0o755, 0o777])
def test_m365_capture_to_first_write_mode_drift_exposes_no_customer_bytes(
    drifted_mode: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    _use_stage(stage, monkeypatch)
    original_open = os.open

    def _open(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == "source.docx" or (isinstance(path, Path) and path.name == "source.docx"):
            stage.chmod(drifted_mode)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    class _MustNotParse:
        async def parse(self, path: Path, *, document_id: str) -> Never:
            del path, document_id
            raise AssertionError("parser must not run after root mode drift")

        async def parse_fd(self, file_fd: int, *, document_id: str, source_name: str) -> Never:
            del file_fd, document_id, source_name
            raise AssertionError("parser must not run after root mode drift")

    monkeypatch.setattr(os, "open", _open)

    async def scenario() -> None:
        with pytest.raises(ParserResidueError) as excinfo:
            await M365BodyParser(_MustNotParse()).parse(
                b"CUSTOMER-BYTES-MUST-NOT-LEAK",
                document_id="doc-1",
                source_format="docx",
            )
        assert type(excinfo.value) is ParserResidueError
        assert str(excinfo.value) == "M365 temporary document cleanup incomplete"
        output = stage / "source.docx"
        assert output.read_bytes() == b""
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
        assert stat.S_IMODE(stage.stat().st_mode) == drifted_mode

    asyncio.run(scenario())


def test_m365_capture_to_first_write_rename_never_writes_outside_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    displaced = tmp_path / "displaced-stage"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "OUTSIDE-SENTINEL-MUST-SURVIVE"
    sentinel.write_text("safe", encoding="utf-8")
    _use_stage(stage, monkeypatch)
    original_open = os.open

    def _open(
        path: str | bytes | Path,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == "source.docx" or (isinstance(path, Path) and path.name == "source.docx"):
            stage.rename(displaced)
            stage.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    class _MustNotParse:
        async def parse(self, path: Path, *, document_id: str) -> Never:
            del path, document_id
            raise AssertionError("parser must not run after root replacement")

        async def parse_fd(self, file_fd: int, *, document_id: str, source_name: str) -> Never:
            del file_fd, document_id, source_name
            raise AssertionError("parser must not run after root replacement")

    monkeypatch.setattr(os, "open", _open)

    async def scenario() -> None:
        with pytest.raises(ParserResidueError) as excinfo:
            await M365BodyParser(_MustNotParse()).parse(
                b"CUSTOMER-BYTES-MUST-NOT-LEAK",
                document_id="doc-1",
                source_format="docx",
            )
        assert type(excinfo.value) is ParserResidueError
        assert str(excinfo.value) == "M365 temporary document cleanup incomplete"
        assert sentinel.read_text(encoding="utf-8") == "safe"
        assert not (outside / "source.docx").exists()
        output = displaced / "source.docx"
        assert output.read_bytes() == b""
        assert stat.S_IMODE(output.stat().st_mode) == 0o600

    asyncio.run(scenario())


def test_m365_private_fd_write_retries_partial_progress_and_fsyncs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    _use_stage(stage, monkeypatch)
    parser = RecordedBodyParser()
    content = b"bounded-private-staging"
    original_write = os.write
    original_fsync = os.fsync
    writes = 0
    fsyncs = 0

    def _partial_write(file_fd: int, data: bytes | memoryview) -> int:
        nonlocal writes
        writes += 1
        return original_write(file_fd, data[:3])

    def _fsync(file_fd: int) -> None:
        nonlocal fsyncs
        fsyncs += 1
        original_fsync(file_fd)

    monkeypatch.setattr(os, "write", _partial_write)
    monkeypatch.setattr(os, "fsync", _fsync)

    async def scenario() -> None:
        payload = await M365BodyParser(parser).parse(
            content,
            document_id="doc-1",
            source_format="docx",
        )
        assert payload["body_ingestion_status"] == "parsed"
        assert payload["body_parser_identity"] == "neutron-m365-document-parser/v1"

    asyncio.run(scenario())

    assert parser.calls == [("doc-1", "docx", content)]
    assert writes > 1
    assert fsyncs == 1
    assert not stage.exists()


def test_m365_prefers_fd_parser_without_reopening_the_staging_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    _use_stage(stage, monkeypatch)
    content = b"fd-only-customer-content"
    inherited_fd: int | None = None

    class _FdOnlyParser:
        async def parse(self, path: Path, *, document_id: str) -> Never:
            del path, document_id
            raise AssertionError("pathname parser fallback must not run")

        async def parse_fd(
            self,
            file_fd: int,
            *,
            document_id: str,
            source_name: str,
        ) -> ParsedDocument:
            nonlocal inherited_fd
            inherited_fd = file_fd
            opened = os.fstat(file_fd)
            assert stat.S_ISREG(opened.st_mode)
            assert stat.S_IMODE(opened.st_mode) == 0o600
            os.lseek(file_fd, 0, os.SEEK_SET)
            assert os.read(file_fd, len(content) + 1) == content
            return ParsedDocument(
                document_id=document_id,
                source_name=source_name,
                source_format="docx",
                blocks=(DocBlock(kind=BlockKind.PARAGRAPH, text="safe parsed text"),),
            )

    async def scenario() -> None:
        payload = await M365BodyParser(_FdOnlyParser()).parse(
            content,
            document_id="doc-1",
            source_format="docx",
        )
        assert payload["body_ingestion_status"] == "parsed"

    asyncio.run(scenario())

    assert not stage.exists()
    assert inherited_fd is not None
    with pytest.raises(OSError):
        os.fstat(inherited_fd)


def test_parser_evidence_sink_runs_only_after_private_stage_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    _use_stage(stage, monkeypatch)
    captured = []
    connector = M365GraphConnector(
        m365_settings(),
        transport=build_m365_transport(),
        parser=RecordedBodyParser(),
    )
    original_sink = connector._body_parser._M365BodyParser__evidence_sink  # type: ignore[reportAttributeAccessIssue]
    assert original_sink is not None

    def sink(source_id, record_id, descriptor) -> None:
        assert not stage.exists()
        captured.append((source_id, record_id, descriptor))
        original_sink(source_id, record_id, descriptor)

    connector._body_parser._M365BodyParser__evidence_sink = sink  # type: ignore[reportAttributeAccessIssue]

    async def scenario() -> None:
        payload = await connector._body_parser.parse(
            b"producer-bound-content",
            document_id="drive-1:item-1",
            record_id="item-1",
            source_format="docx",
            acl=("team",),
        )
        assert (
            connector.take_parsed_evidence(
                "drive-1:item-1",
                "item-1",
                payload,
                ("team",),
                "canonical-item",
            )
            is captured[0][2]
        )

    asyncio.run(scenario())
    assert [(source_id, record_id) for source_id, record_id, _ in captured] == [
        ("drive-1:item-1", "item-1")
    ]


def test_m365_path_only_parser_is_rejected_before_customer_bytes_are_staged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _PathOnlyParser:
        async def parse(self, path: Path, *, document_id: str) -> Never:
            del path, document_id
            raise AssertionError("pathname parser fallback must not run")

    def _must_not_stage(*_args: object, **_kwargs: object) -> Never:
        raise AssertionError("customer bytes must not be staged for a path-only parser")

    monkeypatch.setattr(tempfile, "mkdtemp", _must_not_stage)

    async def scenario() -> None:
        with pytest.raises(ParserEnvironmentError) as excinfo:
            await M365BodyParser(_PathOnlyParser()).parse(
                b"CUSTOMER-BYTES-MUST-NOT-LAND",
                document_id="doc-1",
                source_format="docx",
            )
        assert type(excinfo.value) is ParserEnvironmentError
        assert str(excinfo.value) == "M365 parser fd input unavailable"

    asyncio.run(scenario())


def test_m365_preplanted_input_symlink_is_never_opened_or_followed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "outside-target"
    target.write_bytes(b"outside-bytes-must-not-change")
    target.chmod(0o600)
    before = hashlib.sha256(target.read_bytes()).digest()
    _use_stage(stage, monkeypatch)
    original_capture = m365_content_module._capture_temporary_root

    def _capture(path: Path) -> object:
        root = original_capture(path)
        (stage / "source.docx").symlink_to(target)
        return root

    class _MustNotParse:
        async def parse(self, path: Path, *, document_id: str) -> Never:
            del path, document_id
            raise AssertionError("parser must not run for a preplanted input symlink")

        async def parse_fd(self, file_fd: int, *, document_id: str, source_name: str) -> Never:
            del file_fd, document_id, source_name
            raise AssertionError("parser must not run for a preplanted input symlink")

    monkeypatch.setattr("ne.connect.m365.content._capture_temporary_root", _capture)

    async def scenario() -> None:
        with pytest.raises(ParserEnvironmentError) as excinfo:
            await M365BodyParser(_MustNotParse()).parse(
                b"CUSTOMER-BYTES-MUST-NOT-LAND",
                document_id="doc-1",
                source_format="docx",
            )
        assert type(excinfo.value) is ParserEnvironmentError
        assert str(excinfo.value) == "M365 document staging failed"

    asyncio.run(scenario())

    assert hashlib.sha256(target.read_bytes()).digest() == before
    assert not stage.exists()


def test_m365_open_to_parser_root_replacement_writes_only_bound_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    displaced = tmp_path / "displaced-stage"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "outside-sentinel"
    sentinel.write_bytes(b"outside-bytes-must-not-change")
    before = hashlib.sha256(sentinel.read_bytes()).digest()
    content = b"customer-content-remains-on-bound-fd"
    _use_stage(stage, monkeypatch)

    class _ReplacingFdParser:
        async def parse(self, path: Path, *, document_id: str) -> Never:
            del path, document_id
            raise AssertionError("pathname parser fallback must not run")

        async def parse_fd(
            self,
            file_fd: int,
            *,
            document_id: str,
            source_name: str,
        ) -> ParsedDocument:
            stage.rename(displaced)
            stage.symlink_to(outside, target_is_directory=True)
            os.lseek(file_fd, 0, os.SEEK_SET)
            assert os.read(file_fd, len(content) + 1) == content
            return ParsedDocument(
                document_id=document_id,
                source_name=source_name,
                source_format="docx",
                blocks=(DocBlock(kind=BlockKind.PARAGRAPH, text="safe parsed text"),),
            )

    async def scenario() -> None:
        with pytest.raises(ParserResidueError) as excinfo:
            await M365BodyParser(_ReplacingFdParser()).parse(
                content,
                document_id="doc-1",
                source_format="docx",
            )
        assert type(excinfo.value) is ParserResidueError
        assert str(excinfo.value) == "M365 temporary document cleanup incomplete"

    asyncio.run(scenario())

    assert hashlib.sha256(sentinel.read_bytes()).digest() == before
    assert not (outside / "source.docx").exists()
    assert stage.is_symlink()
    assert displaced.is_dir()
    bound_input = displaced / "source.docx"
    assert bound_input.read_bytes() == content
    assert stat.S_IMODE(bound_input.stat().st_mode) == 0o600


def test_cancelled_m365_docling_worker_never_reads_reused_caller_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    _use_stage(stage, monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    duplicate_closed = threading.Event()
    caller_fd: int | None = None
    worker_fd: int | None = None
    closed_fd: int | None = None
    seen_content: bytes | None = None
    original_open_at = m365_content_module._open_new_file_at
    original_close_owned = docling_parser_module._close_owned_input_fd

    def _record_caller_fd(root: Any, name: str) -> int:
        nonlocal caller_fd
        caller_fd = original_open_at(root, name)
        return caller_fd

    def _barrier_parse(
        _parser_env_dir: Path,
        _venv_dir: Path,
        _path: Path,
        document_id: str,
        *,
        input_fd: int | None = None,
        source_name: str | None = None,
    ) -> ParsedDocument:
        nonlocal worker_fd, seen_content
        assert input_fd is not None
        assert source_name == "source.docx"
        worker_fd = input_fd
        entered.set()
        assert release.wait(timeout=5)
        os.lseek(input_fd, 0, os.SEEK_SET)
        seen_content = os.read(input_fd, 1024)
        return ParsedDocument(
            document_id=document_id,
            source_name=source_name,
            source_format="docx",
            blocks=(DocBlock(kind=BlockKind.PARAGRAPH, text="safe parsed text"),),
        )

    def _record_close(file_fd: int) -> None:
        nonlocal closed_fd
        original_close_owned(file_fd)
        closed_fd = file_fd
        duplicate_closed.set()

    monkeypatch.setattr(m365_content_module, "_open_new_file_at", _record_caller_fd)
    monkeypatch.setattr(docling_parser_module, "_parse_sync", _barrier_parse)
    monkeypatch.setattr(docling_parser_module, "_close_owned_input_fd", _record_close)
    parser = DoclingParser(parser_env_dir=tmp_path / "env", venv_dir=tmp_path / "venv")
    original_content = b"original-customer-bytes"
    replacement = tmp_path / "replacement.bin"
    replacement.write_bytes(b"replacement-request-bytes")

    async def scenario() -> None:
        task = asyncio.create_task(
            M365BodyParser(parser).parse(
                original_content,
                document_id="doc-1",
                source_format="docx",
            )
        )
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 5), timeout=6)
        assert caller_fd is not None
        assert worker_fd is not None
        assert worker_fd != caller_fd

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(OSError):
            os.fstat(caller_fd)

        replacement_fds: list[int] = []
        try:
            for _ in range(64):
                replacement_fd = os.open(replacement, os.O_RDONLY | os.O_CLOEXEC)
                replacement_fds.append(replacement_fd)
                if replacement_fd == caller_fd:
                    break
            assert caller_fd in replacement_fds
            release.set()
            assert await asyncio.wait_for(asyncio.to_thread(duplicate_closed.wait, 5), timeout=6)
            assert seen_content == original_content
            assert closed_fd == worker_fd
            with pytest.raises(OSError):
                os.fstat(worker_fd)
        finally:
            release.set()
            for replacement_fd in replacement_fds:
                os.close(replacement_fd)

    asyncio.run(scenario())

    assert not stage.exists()


def test_staging_cleanup_failure_is_typed_as_parser_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = "UNLINK-PATH-MUST-NOT-LEAK"
    stage = tmp_path / "stage"
    _use_stage(stage, monkeypatch)
    _fail_first_unlink(monkeypatch)

    async def scenario() -> None:
        with pytest.raises(ParserEnvironmentError) as excinfo:
            await M365BodyParser(RecordedBodyParser()).parse(
                b"fixture",
                document_id="doc-1",
                source_format="docx",
            )
        assert type(excinfo.value) is ParserEnvironmentError
        assert str(excinfo.value) == "M365 document staging cleanup failed"
        assert sentinel not in str(excinfo.value)
        assert not stage.exists()

    asyncio.run(scenario())


def test_cleanup_failure_does_not_mask_typed_parser_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser_sentinel = "PARSER-DETAIL-MUST-NOT-LEAK"
    cleanup_sentinel = "UNLINK-PATH-MUST-NOT-LEAK"
    stage = tmp_path / "stage"
    _use_stage(stage, monkeypatch)
    _fail_first_unlink(monkeypatch)

    async def scenario() -> None:
        with pytest.raises(GraphDocumentConversionError) as excinfo:
            await M365BodyParser(_FailingBodyParser(ParserConversionError(parser_sentinel))).parse(
                b"fixture",
                document_id="doc-1",
                source_format="docx",
            )
        assert type(excinfo.value) is GraphDocumentConversionError
        assert str(excinfo.value) == "M365 document conversion failed"
        assert parser_sentinel not in str(excinfo.value)
        assert cleanup_sentinel not in str(excinfo.value)
        assert not stage.exists()

    asyncio.run(scenario())


def test_cleanup_failure_preserves_output_contract_error_and_removes_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_sentinel = "UNLINK-PATH-MUST-NOT-LEAK"
    stage = tmp_path / "stage"
    _use_stage(stage, monkeypatch)
    _fail_first_unlink(monkeypatch)

    async def scenario() -> None:
        with pytest.raises(GraphDocumentOutputError) as excinfo:
            await M365BodyParser(_ContractBodyParser("identity")).parse(
                b"fixture",
                document_id="doc-1",
                source_format="docx",
            )
        assert type(excinfo.value) is GraphDocumentOutputError
        assert str(excinfo.value) == "M365 parser returned a mismatched document identity"
        assert cleanup_sentinel not in str(excinfo.value)
        assert not stage.exists()

    asyncio.run(scenario())


@pytest.mark.parametrize("primary_failure", [False, True])
def test_unremovable_staging_residue_supersedes_success_or_conversion_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    primary_failure: bool,
) -> None:
    stage = tmp_path / "stage"
    _use_stage(stage, monkeypatch)
    _block_all_deletion(monkeypatch)
    parser = (
        _FailingBodyParser(ParserConversionError("PARSER-DETAIL-MUST-NOT-LEAK"))
        if primary_failure
        else RecordedBodyParser()
    )

    async def scenario() -> None:
        with pytest.raises(ParserResidueError) as excinfo:
            await M365BodyParser(parser).parse(
                b"CUSTOMER-BYTES-MUST-NOT-LEAK",
                document_id="doc-1",
                source_format="docx",
            )
        assert type(excinfo.value) is ParserResidueError
        assert isinstance(excinfo.value, ParserEnvironmentError)
        assert str(excinfo.value) == "M365 temporary document cleanup incomplete"
        assert "MUST-NOT-LEAK" not in str(excinfo.value)
        assert (stage / "source.docx").exists()

    asyncio.run(scenario())


def test_unremovable_staging_residue_aborts_sync_with_class_only_audit(
    writer_stack: WriterStack,
    frozen_config: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = ConnectorStateStore()
    stage = tmp_path / "stage"
    _use_stage(stage, monkeypatch)
    _block_all_deletion(monkeypatch)

    async def scenario() -> None:
        with pytest.raises(ParserResidueError) as excinfo:
            await run_m365_sync(
                m365_settings(),
                writer=writer_stack.writer,
                state_store=state,
                audit_sink=writer_stack.sink,
                actor="connector:test",
                transport=build_m365_transport(),
                parser=RecordedBodyParser(),
            )
        assert type(excinfo.value) is ParserResidueError
        assert str(excinfo.value) == "M365 temporary document cleanup incomplete"
        assert state.load_state(_SOURCE) == {}
        await _assert_not_written(writer_stack)
        sync_events = _sync_events(writer_stack)
        assert len(sync_events) == 1
        assert sync_events[0]["config_hash"] == frozen_config
        payload = sync_events[0]["payload"]
        assert isinstance(payload, dict)
        assert payload["detail"] == "mode=backfill error_class=ParserResidueError"
        persisted = json.dumps(_audit_rows(writer_stack), default=str)
        assert "DELETE-PATH-MUST-NOT-LEAK" not in persisted
        assert (stage / "source.docx").exists()

    asyncio.run(scenario())


def test_renamed_staging_root_is_cleaned_by_fd_but_never_reported_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    displaced = tmp_path / "displaced-stage"
    _use_stage(stage, monkeypatch)

    class _RenamingParser:
        async def parse(self, path: Path, *, document_id: str) -> Never:
            del path, document_id
            raise AssertionError("pathname parser fallback must not run")

        async def parse_fd(
            self, file_fd: int, *, document_id: str, source_name: str
        ) -> ParsedDocument:
            parsed = await RecordedBodyParser().parse_fd(
                file_fd, document_id=document_id, source_name=source_name
            )
            stage.rename(displaced)
            return parsed

    async def scenario() -> None:
        with pytest.raises(ParserResidueError) as excinfo:
            await M365BodyParser(_RenamingParser()).parse(
                b"CUSTOMER-BYTES-MUST-NOT-LEAK",
                document_id="doc-1",
                source_format="docx",
            )
        assert type(excinfo.value) is ParserResidueError
        assert str(excinfo.value) == "M365 temporary document cleanup incomplete"
        assert not stage.exists()
        assert displaced.is_dir()
        assert list(displaced.iterdir()) == []

    asyncio.run(scenario())


def test_renamed_staging_root_aborts_sync_with_class_only_audit(
    writer_stack: WriterStack,
    frozen_config: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = ConnectorStateStore()
    stage = tmp_path / "stage"
    displaced = tmp_path / "displaced-stage"
    _use_stage(stage, monkeypatch)

    class _RenamingParser:
        async def parse(self, path: Path, *, document_id: str) -> Never:
            del path, document_id
            raise AssertionError("pathname parser fallback must not run")

        async def parse_fd(
            self, file_fd: int, *, document_id: str, source_name: str
        ) -> ParsedDocument:
            parsed = await RecordedBodyParser().parse_fd(
                file_fd, document_id=document_id, source_name=source_name
            )
            stage.rename(displaced)
            return parsed

    async def scenario() -> None:
        with pytest.raises(ParserResidueError) as excinfo:
            await run_m365_sync(
                m365_settings(),
                writer=writer_stack.writer,
                state_store=state,
                audit_sink=writer_stack.sink,
                actor="connector:test",
                transport=build_m365_transport(),
                parser=_RenamingParser(),
            )
        assert type(excinfo.value) is ParserResidueError
        assert str(excinfo.value) == "M365 temporary document cleanup incomplete"
        assert state.load_state(_SOURCE) == {}
        await _assert_not_written(writer_stack)
        sync_events = _sync_events(writer_stack)
        assert len(sync_events) == 1
        assert sync_events[0]["config_hash"] == frozen_config
        payload = sync_events[0]["payload"]
        assert isinstance(payload, dict)
        assert payload["detail"] == "mode=backfill error_class=ParserResidueError"
        persisted = json.dumps(_audit_rows(writer_stack), default=str)
        assert "CUSTOMER-BYTES-MUST-NOT-LEAK" not in persisted
        assert not stage.exists()
        assert displaced.is_dir()
        assert list(displaced.iterdir()) == []

    asyncio.run(scenario())


def test_replaced_staging_root_is_never_followed_or_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    displaced = tmp_path / "displaced-stage"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "OUTSIDE-SENTINEL-MUST-SURVIVE"
    sentinel.write_text("safe", encoding="utf-8")
    _use_stage(stage, monkeypatch)

    class _ReplacingParser:
        async def parse(self, path: Path, *, document_id: str) -> Never:
            del path, document_id
            raise AssertionError("pathname parser fallback must not run")

        async def parse_fd(
            self, file_fd: int, *, document_id: str, source_name: str
        ) -> ParsedDocument:
            parsed = await RecordedBodyParser().parse_fd(
                file_fd, document_id=document_id, source_name=source_name
            )
            stage.rename(displaced)
            stage.symlink_to(outside, target_is_directory=True)
            return parsed

    async def scenario() -> None:
        with pytest.raises(ParserResidueError) as excinfo:
            await M365BodyParser(_ReplacingParser()).parse(
                b"CUSTOMER-BYTES-MUST-NOT-LEAK",
                document_id="doc-1",
                source_format="docx",
            )
        assert type(excinfo.value) is ParserResidueError
        assert str(excinfo.value) == "M365 temporary document cleanup incomplete"
        assert sentinel.read_text(encoding="utf-8") == "safe"
        assert stage.is_symlink()
        assert (displaced / "source.docx").exists()

    asyncio.run(scenario())


def test_staging_root_replacement_at_cleanup_guard_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    displaced = tmp_path / "displaced-stage"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "OUTSIDE-SENTINEL-MUST-SURVIVE"
    sentinel.write_text("safe", encoding="utf-8")
    _use_stage(stage, monkeypatch)
    original_stat = os.stat
    guarded_calls = 0

    def _stat(
        path: str | bytes | int,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal guarded_calls
        if path == stage.name and dir_fd is not None and not follow_symlinks:
            guarded_calls += 1
            if guarded_calls == 3:
                stage.rename(displaced)
                stage.symlink_to(outside, target_is_directory=True)
        if dir_fd is None:
            return original_stat(path, follow_symlinks=follow_symlinks)
        return original_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "stat", _stat)

    async def scenario() -> None:
        with pytest.raises(ParserResidueError):
            await M365BodyParser(RecordedBodyParser()).parse(
                b"CUSTOMER-BYTES-MUST-NOT-LEAK",
                document_id="doc-1",
                source_format="docx",
            )
        assert guarded_calls >= 3
        assert sentinel.read_text(encoding="utf-8") == "safe"
        assert stage.is_symlink()
        assert (displaced / "source.docx").exists()

    asyncio.run(scenario())


def test_staging_root_rename_at_cleanup_guard_is_never_reported_clean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    displaced = tmp_path / "displaced-stage"
    _use_stage(stage, monkeypatch)
    original_stat = os.stat
    guarded_calls = 0

    def _stat(
        path: str | bytes | int,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal guarded_calls
        if path == stage.name and dir_fd is not None and not follow_symlinks:
            guarded_calls += 1
            if guarded_calls == 3:
                stage.rename(displaced)
        if dir_fd is None:
            return original_stat(path, follow_symlinks=follow_symlinks)
        return original_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "stat", _stat)

    async def scenario() -> None:
        with pytest.raises(ParserResidueError):
            await M365BodyParser(RecordedBodyParser()).parse(
                b"CUSTOMER-BYTES-MUST-NOT-LEAK",
                document_id="doc-1",
                source_format="docx",
            )
        assert guarded_calls >= 3
        assert not stage.exists()
        assert displaced.is_dir()
        assert list(displaced.iterdir()) == []

    asyncio.run(scenario())


@pytest.mark.parametrize("drifted_mode", [0o755, 0o777])
def test_staging_root_mode_drift_is_refused_without_deleting_bytes(
    drifted_mode: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    _use_stage(stage, monkeypatch)

    class _ModeDriftingParser:
        async def parse(self, path: Path, *, document_id: str) -> Never:
            del path, document_id
            raise AssertionError("pathname parser fallback must not run")

        async def parse_fd(
            self, file_fd: int, *, document_id: str, source_name: str
        ) -> ParsedDocument:
            parsed = await RecordedBodyParser().parse_fd(
                file_fd, document_id=document_id, source_name=source_name
            )
            stage.chmod(drifted_mode)
            return parsed

    async def scenario() -> None:
        with pytest.raises(ParserResidueError) as excinfo:
            await M365BodyParser(_ModeDriftingParser()).parse(
                b"CUSTOMER-BYTES-MUST-NOT-LEAK",
                document_id="doc-1",
                source_format="docx",
            )
        assert type(excinfo.value) is ParserResidueError
        assert str(excinfo.value) == "M365 temporary document cleanup incomplete"
        assert stat.S_IMODE(stage.stat().st_mode) == drifted_mode
        assert (stage / "source.docx").exists()

    asyncio.run(scenario())


def test_fd_cleanup_removes_nested_files_and_link_but_not_link_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stage = tmp_path / "stage"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "OUTSIDE-SENTINEL-MUST-SURVIVE"
    sentinel.write_text("safe", encoding="utf-8")
    _use_stage(stage, monkeypatch)

    class _NestedParser:
        async def parse(self, path: Path, *, document_id: str) -> Never:
            del path, document_id
            raise AssertionError("pathname parser fallback must not run")

        async def parse_fd(
            self, file_fd: int, *, document_id: str, source_name: str
        ) -> ParsedDocument:
            parsed = await RecordedBodyParser().parse_fd(
                file_fd, document_id=document_id, source_name=source_name
            )
            nested = stage / "nested"
            nested.mkdir()
            (nested / "ordinary.txt").write_text("delete", encoding="utf-8")
            (stage / "outside-link").symlink_to(outside, target_is_directory=True)
            return parsed

    async def scenario() -> None:
        payload = await M365BodyParser(_NestedParser()).parse(
            b"fixture",
            document_id="doc-1",
            source_format="docx",
        )
        assert payload["body_ingestion_status"] == "parsed"
        assert not stage.exists()
        assert sentinel.read_text(encoding="utf-8") == "safe"

    asyncio.run(scenario())


def test_staging_write_failure_aborts_without_write_and_audits_environment_class(
    writer_stack: WriterStack,
    frozen_config: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = ConnectorStateStore()
    sentinel = "STAGING-WRITE-PATH-MUST-NOT-LEAK"

    def _fail_source_write(_fd: int, _data: bytes | memoryview) -> Never:
        raise OSError(sentinel)

    monkeypatch.setattr(os, "write", _fail_source_write)

    async def scenario() -> None:
        with pytest.raises(ParserEnvironmentError) as excinfo:
            await run_m365_sync(
                m365_settings(),
                writer=writer_stack.writer,
                state_store=state,
                audit_sink=writer_stack.sink,
                actor="connector:test",
                transport=build_m365_transport(),
                parser=RecordedBodyParser(),
            )
        assert type(excinfo.value) is ParserEnvironmentError
        assert str(excinfo.value) == "M365 document staging failed"
        assert state.load_state(_SOURCE) == {}
        await _assert_not_written(writer_stack)
        sync_events = _sync_events(writer_stack)
        assert len(sync_events) == 1
        assert sync_events[0]["config_hash"] == frozen_config
        payload = sync_events[0]["payload"]
        assert isinstance(payload, dict)
        assert payload["detail"] == ("mode=backfill error_class=ParserEnvironmentError")
        assert sentinel not in json.dumps(_audit_rows(writer_stack), default=str)

    asyncio.run(scenario())


@pytest.mark.parametrize("error_type", [ParserEnvironmentError, AirGapViolationError])
def test_parser_infrastructure_and_air_gap_failures_are_not_laundered(
    writer_stack: WriterStack,
    frozen_config: str,
    error_type: type[Exception],
) -> None:
    state = ConnectorStateStore()
    sentinel = "PARSER-INFRASTRUCTURE-DETAIL-MUST-NOT-LEAK"

    async def scenario() -> None:
        with pytest.raises(error_type) as excinfo:
            await run_m365_sync(
                m365_settings(),
                writer=writer_stack.writer,
                state_store=state,
                audit_sink=writer_stack.sink,
                actor="connector:test",
                transport=build_m365_transport(),
                parser=_FailingBodyParser(error_type(sentinel)),
            )
        assert type(excinfo.value) is error_type
        assert sentinel in str(excinfo.value)
        assert state.load_state(_SOURCE) == {}
        await _assert_not_written(writer_stack)
        sync_events = _sync_events(writer_stack)
        assert len(sync_events) == 1
        assert sync_events[0]["config_hash"] == frozen_config
        payload = sync_events[0]["payload"]
        assert isinstance(payload, dict)
        assert payload["detail"] == f"mode=backfill error_class={error_type.__name__}"
        assert sentinel not in json.dumps(_audit_rows(writer_stack), default=str)

    asyncio.run(scenario())


def test_real_docling_runtime_failure_is_not_laundered_by_m365_connector(
    writer_stack: WriterStack,
    frozen_config: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = ConnectorStateStore()
    parser_env_dir = tmp_path / "parser-env"
    venv_dir = tmp_path / "parser-venv"
    parser_env_dir.mkdir()
    runtime = _venv_python_at(venv_dir)
    runtime.parent.mkdir(parents=True)
    runtime_sentinel = "M365-PARSER-RUNTIME-MUST-NOT-LEAK"
    runtime.write_text(runtime_sentinel, encoding="utf-8")
    runtime.chmod(0o600)
    monkeypatch.setattr(docling_parser_module, "ensure_env_at", lambda *_args: None)

    async def scenario() -> None:
        with pytest.raises(ParserEnvironmentError) as excinfo:
            await run_m365_sync(
                m365_settings(),
                writer=writer_stack.writer,
                state_store=state,
                audit_sink=writer_stack.sink,
                actor="connector:test",
                transport=build_m365_transport(),
                parser=DoclingParser(
                    parser_env_dir=parser_env_dir,
                    venv_dir=venv_dir,
                ),
            )
        assert type(excinfo.value) is ParserEnvironmentError
        assert str(excinfo.value) == "parser runtime environment failed"
        assert state.load_state(_SOURCE) == {}
        await _assert_not_written(writer_stack)
        sync_events = _sync_events(writer_stack)
        assert len(sync_events) == 1
        assert sync_events[0]["config_hash"] == frozen_config
        payload = sync_events[0]["payload"]
        assert isinstance(payload, dict)
        assert payload["detail"] == ("mode=backfill error_class=ParserEnvironmentError")
        assert runtime_sentinel not in json.dumps(_audit_rows(writer_stack), default=str)

    asyncio.run(scenario())


def test_content_fetch_failure_aborts_without_metadata_only_write_or_delta_advance(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    state = ConnectorStateStore()

    async def scenario() -> None:
        with pytest.raises(GraphForbiddenError) as excinfo:
            await run_m365_sync(
                m365_settings(),
                writer=writer_stack.writer,
                state_store=state,
                audit_sink=writer_stack.sink,
                actor="connector:test",
                transport=build_m365_transport(content_status_overrides={"item-user": 403}),
                parser=RecordedBodyParser(),
            )
        assert "HTTP 403" in str(excinfo.value)
        assert state.load_state(_SOURCE) == {}
        await _assert_not_written(writer_stack)

    asyncio.run(scenario())


def test_hostile_content_redirect_is_never_followed_and_never_exposed(
    writer_stack: WriterStack, frozen_config: str
) -> None:
    state = ConnectorStateStore()
    recorder: list[httpx.Request] = []
    credential = "REDIRECT-SECRET-MUST-NOT-LEAK"
    hostile = f"http://169.254.169.254/latest/meta-data?tempauth={credential}"

    async def scenario() -> None:
        with pytest.raises(GraphRedirectError) as excinfo:
            await run_m365_sync(
                m365_settings(content_allowed_hosts=("contoso.sharepoint.com",)),
                writer=writer_stack.writer,
                state_store=state,
                audit_sink=writer_stack.sink,
                actor="connector:test",
                transport=build_m365_transport(
                    recorder=recorder,
                    content_redirect_overrides={"item-user": hostile},
                ),
                parser=RecordedBodyParser(),
            )
        message = str(excinfo.value)
        assert "unsafe Graph content redirect" in message
        assert credential not in message
        assert "tempauth" not in message
        assert not any(request.url.host == "169.254.169.254" for request in recorder)
        assert state.load_state(_SOURCE) == {}
        await _assert_not_written(writer_stack)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("limit", "content", "expect_content_request"),
    [
        (12_000, None, False),  # Graph's declared item size is already too large.
        (12_345, b"x" * 12_346, True),  # Stream exceeds the exact admitted ceiling.
    ],
)
def test_oversize_content_aborts_before_write_and_delta_commit(
    writer_stack: WriterStack,
    frozen_config: str,
    limit: int,
    content: bytes | None,
    expect_content_request: bool,
) -> None:
    state = ConnectorStateStore()
    recorder: list[httpx.Request] = []
    content_overrides = {"item-user": content} if content is not None else None
    content_headers = {"item-user": {"Content-Length": str(limit)}} if content is not None else None

    async def scenario() -> None:
        with pytest.raises(GraphContentError) as excinfo:
            await run_m365_sync(
                m365_settings(max_content_bytes=limit),
                writer=writer_stack.writer,
                state_store=state,
                audit_sink=writer_stack.sink,
                actor="connector:test",
                transport=build_m365_transport(
                    recorder=recorder,
                    content_bytes_overrides=content_overrides,
                    content_headers_overrides=content_headers,
                ),
                parser=RecordedBodyParser(),
            )
        assert "oversized" in str(excinfo.value) or "byte limit" in str(excinfo.value)
        content_requests = [
            request for request in recorder if request.url.path.endswith("/content")
        ]
        assert bool(content_requests) is expect_content_request
        assert state.load_state(_SOURCE) == {}
        await _assert_not_written(writer_stack)

    asyncio.run(scenario())
