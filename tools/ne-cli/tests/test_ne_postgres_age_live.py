# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import os
import re
import subprocess
import time
from collections.abc import Sequence
from uuid import uuid4

import pytest

_IMAGE = "ne-postgres-age:18.6-1.7.0"
_PASSWORD = "test-only-age-smoke"
_LIVE_ENABLED = os.environ.get("NE_POSTGRES_AGE_LIVE") == "1"
_LIVE_REQUIRED = os.environ.get("NE_POSTGRES_AGE_LIVE_REQUIRED") == "1"
# This pin exists in FOUR places, and a base bump must move all four together:
#   deploy/docker/ne-postgres-age/Dockerfile  -- the build-time `sha256sum -c -`,
#     which is the source of truth: a wrong value there fails the image build
#   deploy/docker/ne-postgres-age/README.md   -- the recorded inventory
#   tests/test_ne_postgres_age_image.py       -- the static check on both files
#   here                                      -- the runtime check on the built image
# The 2026-08-26 Chainguard bump moved three and missed this one, which is a stale
# pin asserting the PREVIOUS base's inventory against the new image.
_RUNTIME_APK_SHA256 = "ffe2de953ce4575587c9a4083b3af4a7c6832af27248a2ca96ef0e3b8dd6949a"
_LICENSES_SHA256 = "5760b43b0a285c7bf98b9dba4a7bef3bfb35df9c545e564e8c56ed4790fc59da"
_FORBIDDEN_LICENSE = re.compile(
    r"(?:AGPL|SSPL|BUSL|FSL|NONCOMMERCIAL|(?:^|[-.])NC(?:[-.]|$))", re.I
)
_ADMITTED_LICENSE_EXPRESSIONS = frozenset(
    {
        "Apache-2.0",
        "BSD-2-Clause AND GPL-2.0-only",
        "BSD-3-Clause",
        "CC-PDDC",
        "GPL-2.0-only",
        "GPL-2.0-or-later AND LGPL-2.0-or-later",
        "GPL-2.0-or-later AND LGPL-2.0-or-later AND BSD-3-Clause AND MIT",
        "GPL-2.0-or-later AND LGPL-2.1-or-later",
        "GPL-2.0-or-later OR LGPL-2.0-or-later",
        "GPL-3.0-or-later",
        "GPL-3.0-or-later AND GPL-2.0-or-later AND GPL-2.0-only AND GPL-1.0-only AND "
        "LGPL-2.1-or-later AND BSD-1-Clause AND BSD-2-Clause AND BSD-3-Clause AND "
        "BSD-4-Clause-UC AND MIT AND CC-PDDC",
        "GPL-3.0-or-later WITH GCC-exception-3.1",
        "LGPL-2.1",
        "LGPL-2.1-or-later",
        "MIT",
        "MPL-2.0 AND MIT",
        "OLDAP-2.8",
        "blessing",
        "libselinux-1.0",
    }
)


def _validate_runtime_license_inventory(text: str) -> None:
    rows = [line.split("\t") for line in text.splitlines()]
    assert len(rows) == 62
    for row in rows:
        assert len(row) == 4
        package, origin, license_expression, upstream = row
        assert package and origin and license_expression and upstream
        assert not _FORBIDDEN_LICENSE.search(license_expression)
        assert not license_expression.startswith("LicenseRef-")
        assert license_expression in _ADMITTED_LICENSE_EXPRESSIONS


@pytest.mark.parametrize(
    "license_expression",
    [
        "AGPL-3.0",
        "SSPL-1.0",
        "BUSL-1.1",
        "FSL-1.1",
        "CC-BY-NC-4.0",
        "Unreviewed-1.0",
        "Proprietary",
    ],
)
def test_runtime_license_policy_rejects_forbidden_identifiers(license_expression: str) -> None:
    allowed = [
        f"package-{index}=1\torigin\tMIT\thttps://example.invalid/{index}" for index in range(56)
    ]
    observed = "\n".join(
        [*allowed, f"rejected=1\torigin\t{license_expression}\thttps://example.invalid/rejected"]
    )
    with pytest.raises(AssertionError):
        _validate_runtime_license_inventory(observed)


def _run(args: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=check, capture_output=True, text=True)


def _psql(container: str, sql: str) -> str:
    result = _run(
        [
            "docker",
            "exec",
            container,
            "psql",
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-At",
            "-c",
            sql,
        ]
    )
    return result.stdout


@pytest.mark.skipif(
    not _LIVE_ENABLED and not _LIVE_REQUIRED,
    reason="LIVE-PARKED: set NE_POSTGRES_AGE_LIVE=1 after building the exact local image",
)
def test_pg186_age170_extension_gin_and_readback() -> None:
    assert _LIVE_ENABLED, (
        "promoted supply-chain gate must set NE_POSTGRES_AGE_LIVE=1; refusing skip-green"
    )
    suffix = uuid4().hex[:12]
    container = f"ne-age-smoke-{suffix}"
    restarted = f"ne-age-restart-{suffix}"
    volume = f"ne-age-data-{suffix}"
    _run(["docker", "volume", "create", volume])
    _run(
        [
            "docker",
            "run",
            "--detach",
            "--name",
            container,
            "--env",
            f"POSTGRES_PASSWORD={_PASSWORD}",
            "--mount",
            f"type=volume,source={volume},target=/var/lib/postgresql/data",
            _IMAGE,
        ]
    )
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            ready = _run(["docker", "exec", container, "pg_isready", "-U", "postgres"], check=False)
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            logs = _run(["docker", "logs", container], check=False).stderr[-2000:]
            pytest.fail(f"PostgreSQL 18.6 AGE smoke did not become ready: {logs}")

        user_switch = _run(
            [
                "docker",
                "exec",
                container,
                "sh",
                "-ec",
                "gosu postgres id -u",
            ]
        )
        assert user_switch.stdout.strip() == "70"
        inventory = _run(
            [
                "docker",
                "exec",
                container,
                "sh",
                "-ec",
                """
tmp="$(mktemp)"
awk '
BEGIN { RS=""; FS="\\n" }
{
  p=""; v=""; o=""; l=""; u=""
  for (i=1; i<=NF; i++) {
    if ($i ~ /^P:/) p=substr($i,3)
    else if ($i ~ /^V:/) v=substr($i,3)
    else if ($i ~ /^o:/) o=substr($i,3)
    else if ($i ~ /^L:/) l=substr($i,3)
    else if ($i ~ /^U:/) u=substr($i,3)
  }
  if (u == "") u="https://packages.wolfi.dev/os"
  print p "=" v "\\t" o "\\t" l "\\t" u
}' /lib/apk/db/installed | LC_ALL=C sort > "$tmp"
cmp -s "$tmp" /usr/share/ne-postgres-age/runtime-apk-inventory.tsv
test "$(wc -l < "$tmp")" -eq 62
sha256sum \
  /usr/share/ne-postgres-age/runtime-apk-inventory.tsv \
  /usr/share/ne-postgres-age/THIRD_PARTY_LICENSES.txt \
  | cut -d' ' -f1
""",
            ]
        )
        assert inventory.stdout.splitlines() == [_RUNTIME_APK_SHA256, _LICENSES_SHA256]
        runtime_inventory = _run(
            [
                "docker",
                "exec",
                container,
                "cat",
                "/usr/share/ne-postgres-age/runtime-apk-inventory.tsv",
            ]
        )
        _validate_runtime_license_inventory(runtime_inventory.stdout)
        assert _psql(container, "SHOW server_version;").strip() == "18.6"
        _psql(container, "CREATE EXTENSION age;")
        assert (
            _psql(container, "SELECT extversion FROM pg_extension WHERE extname = 'age';").strip()
            == "1.7.0"
        )
        _psql(
            container,
            "LOAD 'age'; SET search_path = ag_catalog, public; SELECT create_graph('ne_smoke');",
        )
        _psql(
            container,
            "LOAD 'age'; SET search_path = ag_catalog, public; "
            "SELECT * FROM cypher('ne_smoke', $$ "
            "CREATE (:Account {key: 'acct-1', acl: ['all-employees']}) "
            "$$) AS (created agtype);",
        )
        _psql(
            container,
            'CREATE INDEX account_properties_gin ON "ne_smoke"."Account" '
            'USING gin (properties); ANALYZE "ne_smoke"."Account";',
        )
        assert (
            _psql(
                container,
                "SELECT indexdef FROM pg_indexes WHERE schemaname = 'ne_smoke' "
                "AND indexname = 'account_properties_gin';",
            )
            .strip()
            .endswith("USING gin (properties)")
        )
        observed = _psql(
            container,
            "LOAD 'age'; SET search_path = ag_catalog, public; "
            "SELECT * FROM cypher('ne_smoke', $$ "
            "MATCH (n:Account {key: 'acct-1'}) RETURN n.key, n.acl "
            "$$) AS (key agtype, acl agtype);",
        )
        assert '"acct-1"|["all-employees"]' in observed

        _run(["docker", "rm", "--force", container])
        _run(
            [
                "docker",
                "run",
                "--detach",
                "--name",
                restarted,
                "--env",
                f"POSTGRES_PASSWORD={_PASSWORD}",
                "--mount",
                f"type=volume,source={volume},target=/var/lib/postgresql/data",
                _IMAGE,
            ]
        )
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            ready = _run(["docker", "exec", restarted, "pg_isready", "-U", "postgres"], check=False)
            if ready.returncode == 0:
                break
            time.sleep(1)
        else:
            logs = _run(["docker", "logs", restarted], check=False).stderr[-2000:]
            pytest.fail(f"restarted PostgreSQL 18.6 AGE did not become ready: {logs}")

        persisted = _psql(
            restarted,
            "LOAD 'age'; SET search_path = ag_catalog, public; "
            "SELECT * FROM cypher('ne_smoke', $$ "
            "MATCH (n:Account {key: 'acct-1'}) RETURN n.key, n.acl "
            "$$) AS (key agtype, acl agtype);",
        )
        assert '"acct-1"|["all-employees"]' in persisted
    finally:
        _run(["docker", "rm", "--force", container], check=False)
        _run(["docker", "rm", "--force", restarted], check=False)
        _run(["docker", "volume", "rm", volume], check=False)
