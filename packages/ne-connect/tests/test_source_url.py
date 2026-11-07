# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Server-authoritative connector source-link validation."""

from __future__ import annotations

import pytest

from ne.connect.source_url import (
    canonical_external_source_url,
    salesforce_record_source_url,
)


def _nested_escape(value: str, depth: int) -> str:
    for _ in range(depth):
        value = value.replace("%", "%25")
    return value


def test_external_source_url_is_canonical_and_exact_host_bound() -> None:
    assert (
        canonical_external_source_url(
            "https://TENANT.sharepoint.com/sites/ops/Shared%20Documents/SOP.docx/",
            allowed_hosts=("tenant.sharepoint.com",),
        )
        == "https://tenant.sharepoint.com/sites/ops/Shared%20Documents/SOP.docx"
    )


@pytest.mark.parametrize(
    "value, allowed_hosts",
    [
        (None, ("tenant.sharepoint.com",)),
        ("https://tenant.sharepoint.com/sites/ops/SOP.docx", ()),
        ("http://tenant.sharepoint.com/sites/ops/SOP.docx", ("tenant.sharepoint.com",)),
        (
            "https://user:secret@tenant.sharepoint.com/sites/ops/SOP.docx",
            ("tenant.sharepoint.com",),
        ),
        ("https://evil.example/sites/ops/SOP.docx", ("tenant.sharepoint.com",)),
        (
            "https://tenant.sharepoint.com/sites/ops/SOP.docx?tempauth=secret",
            ("tenant.sharepoint.com",),
        ),
        (
            "https://tenant.sharepoint.com/sites/ops/SOP.docx#fragment",
            ("tenant.sharepoint.com",),
        ),
        ("https://tenant.sharepoint.com:8443/sites/ops/SOP.docx", ("tenant.sharepoint.com",)),
        ("https://tenant.sharepoint.com/", ("tenant.sharepoint.com",)),
        ("https://tenant.sharepoint.com/sites/../secret", ("tenant.sharepoint.com",)),
        ("https://tenant.sharepoint.com/sites/%2e%2e/secret", ("tenant.sharepoint.com",)),
        ("https://tenant.sharepoint.com/sites/%2F%2Fevil", ("tenant.sharepoint.com",)),
        ("https://tenant.sharepoint.com/sites/%ZZ/evil", ("tenant.sharepoint.com",)),
        ("https://tenant.sharepoint.com/sites/ops\\evil", ("tenant.sharepoint.com",)),
        ("https://tenant.sharepoint.com/sites/a\tb", ("tenant.sharepoint.com",)),
        ("https://tenant.sharepoint.com/sites/a\nb", ("tenant.sharepoint.com",)),
        ("https://tenant.sharepoint.com/sites/a\rb", ("tenant.sharepoint.com",)),
        ("https://tenant.sharepoint.com/sites/a\x7fb", ("tenant.sharepoint.com",)),
        ("https://tenant.sharepoint.com/sites/%252e%252e/secret", ("tenant.sharepoint.com",)),
        ("https://tenant.sharepoint.com/sites/%252f%252fevil", ("tenant.sharepoint.com",)),
        ("https://tenant.sharepoint.com/sites/%255cevil", ("tenant.sharepoint.com",)),
        ("https://tenant.sharepoint.com/sites/%2500evil", ("tenant.sharepoint.com",)),
        ("https://tenant.sharepoint.com/sites/%25252e%25252e/secret", ("tenant.sharepoint.com",)),
        ("https://tenant.sharepoint.com/sites/%25252f%25252fevil", ("tenant.sharepoint.com",)),
        ("https://tenant.sharepoint.com/sites/%25255cevil", ("tenant.sharepoint.com",)),
        ("https://tenant.sharepoint.com/sites/%252500evil", ("tenant.sharepoint.com",)),
        (
            "https://tenant.sharepoint.com/sites/%2525252e%2525252e/secret",
            ("tenant.sharepoint.com",),
        ),
        ("https://tenant.sharepoint.com/sites/%2525252f%2525252fevil", ("tenant.sharepoint.com",)),
        ("https://tenant.sharepoint.com/sites/%2525255cevil", ("tenant.sharepoint.com",)),
        ("https://tenant.sharepoint.com/sites/%25252500evil", ("tenant.sharepoint.com",)),
        (
            "https://tenant.sharepoint.com/sites/" + _nested_escape("%2e%2e", 10) + "/secret",
            ("tenant.sharepoint.com",),
        ),
        (
            "https://tenant.sharepoint.com/sites/" + _nested_escape("%2f%2fevil", 10),
            ("tenant.sharepoint.com",),
        ),
        (
            "https://tenant.sharepoint.com/sites/" + _nested_escape("%5cevil", 10),
            ("tenant.sharepoint.com",),
        ),
        (
            "https://tenant.sharepoint.com/sites/" + _nested_escape("%00evil", 10),
            ("tenant.sharepoint.com",),
        ),
    ],
)
def test_external_source_url_rejects_unsafe_or_ambiguous_values(
    value: object, allowed_hosts: tuple[str, ...]
) -> None:
    assert canonical_external_source_url(value, allowed_hosts=allowed_hosts) is None


def test_salesforce_record_url_uses_validated_origin_and_canonical_ids() -> None:
    assert (
        salesforce_record_source_url(
            instance_url="https://Acme.my.salesforce.com/",
            instance_allowed_hosts=(".my.salesforce.com",),
            object_name="Dispute__c",
            record_id="a01000000000001AAA",
        )
        == "https://acme.my.salesforce.com/lightning/r/Dispute__c/a01000000000001AAA/view"
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"instance_url": "https://evil.example"},
        {"instance_url": "https://acme.my.salesforce.com/path"},
        {"instance_url": "https://acme.my.salesforce.com?token=secret"},
        {"instance_allowed_hosts": ()},
        {"object_name": "Account/../../User"},
        {"record_id": "not-a-salesforce-id"},
    ],
)
def test_salesforce_record_url_omits_untrusted_or_noncanonical_inputs(
    overrides: dict[str, object],
) -> None:
    values = {
        "instance_url": "https://acme.my.salesforce.com",
        "instance_allowed_hosts": (".my.salesforce.com",),
        "object_name": "Account",
        "record_id": "001000000000001AAA",
    }
    values.update(overrides)
    assert salesforce_record_source_url(**values) is None  # type: ignore[arg-type]
