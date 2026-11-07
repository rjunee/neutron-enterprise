# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Byte-stable, dependency-free DOCX/PDF and text artifact construction."""

from __future__ import annotations

import csv
import io
import json
from xml.sax.saxutils import escape
from zipfile import ZIP_STORED, ZipFile, ZipInfo

_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
_PACKAGE_RELS = "http://schemas.openxmlformats.org/package/2006/relationships"
_OFFICE_RELS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_CT_RELS = "application/vnd.openxmlformats-package.relationships+xml"
_CT_DOCUMENT = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
_CT_CORE = "application/vnd.openxmlformats-package.core-properties+xml"
_CT_APP = "application/vnd.openxmlformats-officedocument.extended-properties+xml"
_NS_CORE = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"


def _zip_entry(name: str, body: str) -> tuple[ZipInfo, bytes]:
    info = ZipInfo(name, date_time=_ZIP_TIME)
    info.compress_type = ZIP_STORED
    info.create_system = 0
    info.external_attr = 0
    return info, body.encode("utf-8")


def build_docx(title: str, paragraphs: list[str], *, version: str, effective: str) -> bytes:
    """Return a minimal valid WordprocessingML document with stable ZIP bytes."""
    document_paragraphs = "".join(
        f'<w:p><w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'
        for text in [title, f"Version {version}", f"Effective {effective}", *paragraphs]
    )
    files = (
        (
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            f'<Default Extension="rels" ContentType="{_CT_RELS}"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            f'<Override PartName="/word/document.xml" ContentType="{_CT_DOCUMENT}"/>'
            f'<Override PartName="/docProps/core.xml" ContentType="{_CT_CORE}"/>'
            f'<Override PartName="/docProps/app.xml" ContentType="{_CT_APP}"/>'
            "</Types>",
        ),
        (
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{_PACKAGE_RELS}">'
            f'<Relationship Id="rId1" Type="{_OFFICE_RELS}/officeDocument" '
            'Target="word/document.xml"/>'
            f'<Relationship Id="rId2" Type="{_PACKAGE_RELS}/metadata/core-properties" '
            'Target="docProps/core.xml"/>'
            f'<Relationship Id="rId3" Type="{_OFFICE_RELS}/extended-properties" '
            'Target="docProps/app.xml"/>'
            "</Relationships>",
        ),
        (
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{document_paragraphs}<w:sectPr/></w:body></w:document>",
        ),
        (
            "docProps/core.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<cp:coreProperties xmlns:cp="{_NS_CORE}" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:dcterms="http://purl.org/dc/terms/" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            f"<dc:title>{escape(title)}</dc:title><dc:creator>Meridian Bank</dc:creator>"
            '<dcterms:created xsi:type="dcterms:W3CDTF">2026-08-18T00:00:00Z</dcterms:created>'
            '<dcterms:modified xsi:type="dcterms:W3CDTF">2026-08-18T00:00:00Z</dcterms:modified>'
            "</cp:coreProperties>",
        ),
        (
            "docProps/app.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
            "<Application>Meridian Fixture Generator</Application></Properties>",
        ),
    )
    out = io.BytesIO()
    with ZipFile(out, "w") as archive:
        for name, body in files:
            info, data = _zip_entry(name, body)
            archive.writestr(info, data)
    return out.getvalue()


def build_pdf(title: str, lines: list[str]) -> bytes:
    """Build a single-page, deterministic PDF 1.4 using only standard fonts."""

    def pdf_text(value: str) -> str:
        return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    commands = ["BT", "/F1 12 Tf", "72 740 Td", f"({pdf_text(title)}) Tj"]
    for line in lines:
        commands.extend(("0 -20 Td", f"({pdf_text(line)}) Tj"))
    commands.append("ET")
    # NUL is valid PDF content-stream whitespace and makes the generated file
    # unambiguously binary to source-control tooling as its header declares.
    stream = ("\n".join(commands) + "\n").encode("ascii") + b"\x00"
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        (
            b"<< /Length "
            + str(len(stream)).encode("ascii")
            + b" >>\nstream\n"
            + stream
            + b"endstream"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        (
            b"<< /Title ("
            + pdf_text(title).encode("ascii")
            + b") /Producer (Meridian Fixture Generator) "
            + b"/CreationDate (D:20260818000000Z) >>"
        ),
    )
    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for number, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{number} 0 obj\n".encode("ascii"))
        output.extend(obj)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info 6 0 R "
        "/ID [<6d6572696469616e2d76312d70646621><6d6572696469616e2d76312d70646621>] >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    return bytes(output)


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode("utf-8")


def csv_bytes(rows: list[list[str]]) -> bytes:
    out = io.StringIO(newline="")
    writer = csv.writer(out, lineterminator="\n")
    writer.writerows(rows)
    return out.getvalue().encode("utf-8")
