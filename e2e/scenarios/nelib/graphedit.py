# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""The S8 SharePoint edit: flip the SOP's standard-tier number via MS Graph.

S8 (temporal provenance) needs a REAL document edit between Answer A and
Answer B. On stage a human does it in Word online; here the suite does it with
an app-only Graph client-credentials grant (client secret) and a byte-level
docx rewrite: download the file, replace ``within {n} business days`` with
``within {new} business days`` inside ``word/document.xml``, upload it back.

HONEST LIMITS (also in e2e/README.md):

* Requires a Graph app registration with ``Files.ReadWrite.All`` (or
  ``Sites.ReadWrite.All``) APPLICATION permission on the tenant. The platform
  connector's own registration is read-scoped for sync plus mail-scoped for
  the S5 ladder; whether it also carries a Files WRITE scope is a tenant-admin
  fact this repo cannot prove. Configure ``NE_E2E_GRAPH_*`` accordingly.
* Word can split a phrase across XML runs. A doc last saved by Word online may
  not contain the literal phrase as contiguous bytes; in that case this module
  FAILS LOUDLY (it never uploads a doc it could not edit) and the failure
  message says to make one manual Word-online edit of the phrase, after which
  the byte-contiguous form written here survives future round trips.
* The edit walks the document version FORWARD, permanently. There is no reset
  path (lineage is append-only by design). The suite alternates 10 <-> 7 so
  repeated runs stay semantically stable, but version numbers only ever grow —
  callers must assert monotonicity, never absolute version numbers.
"""

from __future__ import annotations

import io
import re
import urllib.parse
import zipfile
from dataclasses import dataclass

import httpx

_LOGIN_BASE = "https://login.microsoftonline.com"
_GRAPH_BASE = "https://graph.microsoft.com/v1.0"


@dataclass(frozen=True)
class GraphEditConfig:
    tenant_id: str
    client_id: str
    client_secret: str
    drive_id: str
    item_path: str  # e.g. "Debit Card Dispute Handling SOP.docx" (drive-root relative)


def _token(config: GraphEditConfig, http: httpx.Client) -> str:
    response = http.post(
        f"{_LOGIN_BASE}/{config.tenant_id}/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials",
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "scope": "https://graph.microsoft.com/.default",
        },
    )
    response.raise_for_status()
    return str(response.json()["access_token"])


def _item_url(config: GraphEditConfig) -> str:
    quoted = urllib.parse.quote(config.item_path)
    return f"{_GRAPH_BASE}/drives/{config.drive_id}/root:/{quoted}:"


def flip_standard_tier_days(config: GraphEditConfig, *, current: int, new: int) -> None:
    """Download the SOP, swap ``within {current}`` for ``within {new}``, upload.

    Raises ``AssertionError`` with a remediation message when the contiguous
    phrase is not found (Word run-splitting) — nothing is uploaded in that case.
    """
    with httpx.Client(timeout=120.0) as http:
        headers = {"Authorization": f"Bearer {_token(config, http)}"}
        item = _item_url(config)
        content = http.get(f"{item}/content", headers=headers, follow_redirects=True)
        content.raise_for_status()
        edited = _rewrite_docx(content.content, current=current, new=new)
        upload = http.put(
            f"{item}/content",
            headers={
                **headers,
                "Content-Type": (
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                ),
            },
            content=edited,
        )
        upload.raise_for_status()


def _rewrite_docx(raw: bytes, *, current: int, new: int) -> bytes:
    needle = f"within {current} business days"
    replacement = f"within {new} business days"
    source = zipfile.ZipFile(io.BytesIO(raw))
    document = source.read("word/document.xml").decode("utf-8")
    if needle not in document:
        split_hint = re.search(rf"within[^a-zA-Z0-9<]*(<[^>]+>\s*)*{current}", document)
        raise AssertionError(
            f"S8 edit: the phrase {needle!r} is not byte-contiguous in word/document.xml"
            + (" (Word run-splitting detected)" if split_hint else "")
            + ". Nothing was uploaded. Remediation: make ONE manual Word-online edit of the"
            " standard-tier phrase so it re-serialises as a single run, then re-run the suite."
        )
    document = document.replace(needle, replacement)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            data = (
                document.encode("utf-8")
                if info.filename == "word/document.xml"
                else source.read(info.filename)
            )
            target.writestr(info, data)
    return output.getvalue()
