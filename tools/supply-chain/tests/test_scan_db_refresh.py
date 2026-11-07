# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Guards for tools/supply-chain/scan-db-refresh.sh.

Background: on 2026-08-20 the Mac hit a 95% disk alert with 21 GB of grype
cache. grype stages each download in `db/grype-db-download<random>` and moves
it onto `db/6` on success; a run killed mid-download leaves the staging copy
behind forever, because the random suffix is never reused.

The dangerous failure mode of any fix here is deleting the LIVE database, so
most of these tests assert what the script must NOT delete. The script runs
against fake `grype`/`trivy` shims on PATH, which lets us drive the failure
branches (update fails, status unreadable, DB invalid) that we cannot trigger
against the real binaries.
"""

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scan-db-refresh.sh"
OLD_MTIME = (1755561600, 1755561600)  # 2026-08-19, comfortably past the age gate


def _shim(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    p.chmod(0o755)


@pytest.fixture
def env(tmp_path):
    """A fake grype+trivy cache with shims that report a healthy live DB."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    grype_cache = tmp_path / "grype"
    trivy_cache = tmp_path / "trivy"
    live = grype_cache / "db" / "6"
    live.mkdir(parents=True)
    (live / "vulnerability.db").write_bytes(b"LIVE" * 256)
    # Age the live directory deliberately. In production db/6 holds yesterday's
    # download and is well past the staging age gate, so the ONLY thing keeping
    # it alive is the explicit live-dir skip. A freshly-mtimed live dir would be
    # protected by the age gate instead, and every "we never delete the live DB"
    # assertion below would pass for the wrong reason.
    os.utime(live, OLD_MTIME)
    (trivy_cache / "db").mkdir(parents=True)
    (trivy_cache / "db" / "trivy.db").write_bytes(b"T" * 128)

    _shim(
        bin_dir,
        "grype",
        f"""
        if [ "$1" = "db" ] && [ "$2" = "status" ]; then
          echo '{{"schemaVersion":"v6.1.9","path":"{live}/vulnerability.db","valid":true}}'
          exit 0
        fi
        exit 0
    """,
    )
    _shim(
        bin_dir,
        "trivy",
        """
        exit 0
    """,
    )

    e = dict(os.environ)
    e["PATH"] = f"{bin_dir}:{e['PATH']}"
    e["GRYPE_DB_CACHE_DIR"] = str(grype_cache)
    e["TRIVY_CACHE_DIR"] = str(trivy_cache)
    return {
        "env": e,
        "bin": bin_dir,
        "grype_cache": grype_cache,
        "trivy_cache": trivy_cache,
        "live": live,
        "tmp": tmp_path,
    }


def run(env, *args):
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env["env"],
        capture_output=True,
        text=True,
    )


def orphan(env, name="grype-db-download123456", *, aged=True, size=4096):
    """A staging directory left behind by an interrupted download.

    An abandoned download is untouched from the moment the run died, so the
    CONTENTS must be aged as well as the directory. Ageing only the directory
    would model a download that is still being written, which the script
    deliberately refuses to delete.
    """
    d = env["grype_cache"] / "db" / name
    d.mkdir(parents=True, exist_ok=True)
    f = d / "vulnerability.db"
    f.write_bytes(b"X" * size)
    if aged:
        os.utime(f, OLD_MTIME)
        os.utime(d, OLD_MTIME)
    return d


# --- the core behaviour -------------------------------------------------


def test_orphaned_staging_dir_is_deleted(env):
    o = orphan(env)
    r = run(env, "--prune-only")
    assert r.returncode == 0, r.stderr
    assert not o.exists(), "the orphaned staging directory survived the prune"


def test_live_database_is_never_deleted(env):
    orphan(env)
    run(env, "--prune-only")
    assert (env["live"] / "vulnerability.db").exists(), "the LIVE database was deleted"


def test_stale_schema_directory_is_deleted(env):
    """A grype major-version upgrade strands the old schema dir (e.g. db/5)."""
    stale = env["grype_cache"] / "db" / "5"
    stale.mkdir(parents=True)
    old_db = stale / "vulnerability.db"
    old_db.write_bytes(b"OLD")
    os.utime(old_db, OLD_MTIME)
    os.utime(stale, OLD_MTIME)
    run(env, "--prune-only")
    assert not stale.exists()


def test_recent_staging_dir_survives_as_a_concurrent_download(env):
    """A staging dir being written right now belongs to a running scan.

    Deleting it would break a concurrent run, so the age gate must protect it.
    """
    fresh = orphan(env, "grype-db-download999999", aged=False)
    run(env, "--prune-only")
    assert fresh.exists(), "an in-flight download's staging dir was deleted"


# --- fail-safe: when in doubt, delete nothing ---------------------------


def test_no_prune_when_grype_reports_invalid_database(env):
    # The path must EXIST and be the real live DB, so the only guard that can
    # stop the prune is the `valid:false` check itself. Pointing at a missing
    # file would let the separate "path does not resolve" guard pass this test
    # even with the validity check deleted.
    o = orphan(env)
    _shim(
        env["bin"],
        "grype",
        f"""
        if [ "$1" = "db" ] && [ "$2" = "status" ]; then
          echo '{{"path":"{env["live"]}/vulnerability.db","valid":false}}'; exit 0
        fi
        exit 0
    """,
    )
    r = run(env, "--prune-only")
    assert o.exists(), "pruned while the DB was reported invalid"
    assert "NOT valid" in r.stdout, "a different guard fired; the validity check was not exercised"


def test_no_prune_when_status_command_fails(env):
    o = orphan(env)
    _shim(
        env["bin"],
        "grype",
        """
        if [ "$1" = "db" ] && [ "$2" = "status" ]; then exit 1; fi
        exit 0
    """,
    )
    r = run(env, "--prune-only")
    assert o.exists(), "pruned without being able to read which DB is live"
    assert "status' failed" in r.stdout, (
        "a different guard fired; the status-failure branch was not exercised"
    )


def test_no_prune_when_the_update_fails(env):
    """The owner's explicit ordering constraint: prune only AFTER a good download.

    A failed update must never be followed by deletion, or a network blip
    leaves the lane with no database at all.
    """
    o = orphan(env)
    _shim(
        env["bin"],
        "grype",
        f"""
        if [ "$1" = "db" ] && [ "$2" = "update" ]; then exit 1; fi
        if [ "$1" = "db" ] && [ "$2" = "status" ]; then
          echo '{{"path":"{env["live"]}/vulnerability.db","valid":true}}'; exit 0
        fi
        exit 0
    """,
    )
    r = run(env)  # NOT --prune-only: exercise the real update path
    assert o.exists(), "pruned after a FAILED update"
    assert "did not succeed" in r.stdout


# --- the wrong tools, asserted against the source -----------------------


def test_does_not_use_grype_db_delete():
    """`grype db delete` removes the LIVE db and forces a ~90s re-download."""
    src = SCRIPT.read_text()
    executable = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
    assert "db delete" not in executable


def test_does_not_wipe_the_trivy_vulnerability_database():
    """trivy replaces trivy.db in place, so --vuln-db/--all only costs a redownload."""
    src = SCRIPT.read_text()
    executable = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
    assert "--vuln-db" not in executable
    assert "clean --all" not in executable and "--all" not in executable


# --- visibility ---------------------------------------------------------


def test_emits_a_machine_readable_footprint_line(env):
    r = run(env, "--prune-only")
    line = [x for x in r.stdout.splitlines() if x.startswith("SCAN_CACHE_BYTES")]
    assert line, f"no footprint line in:\n{r.stdout}"
    for field in ("grype=", "trivy=", "total=", "pruned=", "limit="):
        assert field in line[0]


def test_reports_bytes_actually_reclaimed(env):
    orphan(env, size=200_000)
    r = run(env, "--prune-only")
    line = next(x for x in r.stdout.splitlines() if x.startswith("SCAN_CACHE_BYTES"))
    pruned = int(dict(kv.split("=") for kv in line.split()[1:])["pruned"])
    assert pruned > 0, "reclaimed bytes reported as zero after deleting a real directory"


def test_over_budget_is_reported_but_does_not_fail_by_default(env):
    """A disk anomaly must never suppress the nightly vulnerability scan."""
    env["env"]["NE_SCAN_CACHE_MAX_BYTES"] = "1"
    r = run(env, "--prune-only")
    assert r.returncode == 0
    assert "SCAN_CACHE_OVER_BUDGET" in r.stderr


def test_over_budget_fails_under_enforce(env):
    env["env"]["NE_SCAN_CACHE_MAX_BYTES"] = "1"
    r = run(env, "--prune-only", "--enforce")
    assert r.returncode == 3
    assert "SCAN_CACHE_OVER_BUDGET" in r.stderr


def test_unknown_argument_is_rejected(env):
    r = run(env, "--delete-everything")
    assert r.returncode == 2


@pytest.mark.skipif(not shutil.which("bash"), reason="bash required")
def test_script_parses():
    r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# --- regressions from the cross-model review (2026-08-20) ---------------


def test_explicitly_configured_cache_dir_is_used_for_pruning(tmp_path):
    """An explicit GRYPE_DB_CACHE_DIR must be honoured even before it exists.

    The old code fell back to ~/.cache/grype whenever the configured directory
    was missing at startup -- which is the normal state on a clean first run,
    because the scanner only creates it during the update that follows. The
    scanner honours the exported variable, so update wrote to one tree while
    prune and footprint reporting operated on another: the prune silently never
    ran and the reported footprint described the wrong cache.

    Modelled faithfully: the configured directory does NOT exist when the
    script starts, and the grype shim creates it during `db update`.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    configured = tmp_path / "configured-grype"  # absent at startup, by design
    home = tmp_path / "home"
    (home / ".cache" / "grype" / "db").mkdir(parents=True)

    live = configured / "db" / "6" / "vulnerability.db"
    stale = configured / "db" / "grype-db-download4242"

    _shim(
        bin_dir,
        "grype",
        f"""
        if [ "$1" = "db" ] && [ "$2" = "update" ]; then
          mkdir -p "{live.parent}" "{stale}"
          echo LIVE > "{live}"
          echo OLD > "{stale}/vulnerability.db"
          touch -t 202608190000 "{stale}/vulnerability.db" "{stale}"
          exit 0
        fi
        if [ "$1" = "db" ] && [ "$2" = "status" ]; then
          echo '{{"path":"{live}","valid":true}}'; exit 0
        fi
        exit 0
    """,
    )
    _shim(bin_dir, "trivy", "exit 0\n")

    e = dict(os.environ)
    e["PATH"] = f"{bin_dir}:{e['PATH']}"
    e["HOME"] = str(home)
    e["GRYPE_DB_CACHE_DIR"] = str(configured)
    e["TRIVY_CACHE_DIR"] = str(tmp_path / "configured-trivy")
    r = subprocess.run(["bash", str(SCRIPT)], env=e, capture_output=True, text=True)
    assert live.exists(), "deleted the live DB in the configured cache"
    assert not stale.exists(), (
        "the orphan in the CONFIGURED cache was not pruned -- the script was "
        f"operating on a different tree.\n{r.stdout}"
    )


def test_live_db_stored_directly_under_db_is_preserved(env):
    """Some layouts report the live file at `db/vulnerability.db`.

    Then the live file's parent IS the cache root, so no first-level entry can
    equal it and the live database would be deleted as a sibling.
    """
    root = env["grype_cache"] / "db"
    flat = root / "vulnerability.db"
    flat.write_bytes(b"FLAT-LIVE")
    os.utime(flat, OLD_MTIME)
    os.utime(root, OLD_MTIME)
    _shim(
        env["bin"],
        "grype",
        f"""
        if [ "$1" = "db" ] && [ "$2" = "status" ]; then
          echo '{{"path":"{flat}","valid":true}}'; exit 0
        fi
        exit 0
    """,
    )
    run(env, "--prune-only")
    assert flat.exists(), "deleted the live database under a flat layout"


def test_live_db_outside_the_cache_root_stops_the_prune(env):
    """If the reported live DB is not inside the tree we can see, delete nothing."""
    outside = env["tmp"] / "elsewhere" / "db" / "6"
    outside.mkdir(parents=True)
    (outside / "vulnerability.db").write_bytes(b"LIVE")
    o = orphan(env)
    _shim(
        env["bin"],
        "grype",
        f"""
        if [ "$1" = "db" ] && [ "$2" = "status" ]; then
          echo '{{"path":"{outside}/vulnerability.db","valid":true}}'; exit 0
        fi
        exit 0
    """,
    )
    r = run(env, "--prune-only")
    assert o.exists(), "pruned a tree that does not contain the live database"
    assert "not inside the cache root" in r.stdout


def test_download_still_writing_is_not_deleted_despite_an_old_directory_mtime(env):
    """The concurrency guarantee must hold for a LONG download.

    A directory's mtime stops advancing once its entries exist, so a staging
    dir created hours ago whose .db file is still growing looks abandoned by
    directory mtime alone. Deleting it would destroy an in-flight download --
    exactly what the script promises never to do.
    """
    d = env["grype_cache"] / "db" / "grype-db-download555555"
    d.mkdir(parents=True)
    part = d / "vulnerability.db"
    part.write_bytes(b"PARTIAL")
    os.utime(d, OLD_MTIME)  # directory looks old...
    # ...but the file inside was just written, i.e. the download is live.
    run(env, "--prune-only")
    assert d.exists(), "deleted a download that was still being written"
