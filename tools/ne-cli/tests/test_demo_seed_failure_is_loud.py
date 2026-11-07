# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""NE-CONNECT-SEEDLOCK / INBOX 26: an unseeded corpus CANNOT be missed.

For six consecutive bring-ups ``ne demo up`` printed two ``ERROR ... database is
locked`` lines and moved on. Two corpora went unseeded every time and it read as
cosmetic. It was not: ``fixture`` IS the Acme corpus the whole demo arc queries and
``csv`` is the renewals sheet behind the quarantine exhibit, so the box came up with
a half-seeded store.

These tests pin the two halves of the fix that make that impossible to repeat:

* ``ConnectorSyncFailed`` NAMES the sources, so the bring-up does not have to scrape
  stderr to know what it lost (the reason it settled for a warning before).
* ``_report_seed_failure`` prints an unmissable banner naming each corpus AND its
  stakes, and exits NON-ZERO — after the stack is up, so the box stays usable.
"""

from __future__ import annotations

import importlib
import inspect
from pathlib import Path

import pytest

connect_cli = importlib.import_module("ne.cli.connect")
live = importlib.import_module("ne.cli.demo.live")


def test_connector_sync_failed_names_its_sources_and_still_exits_1() -> None:
    exc = connect_cli.ConnectorSyncFailed(["fixture", "csv"])
    assert exc.sources == ("fixture", "csv")
    # A shell caller sees exactly what it saw before: exit 1.
    assert exc.code == 1
    assert isinstance(exc, SystemExit)


def test_report_seed_failure_is_loud_names_the_stakes_and_exits_nonzero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        live._report_seed_failure(("fixture", "csv"), tmp_path)
    assert excinfo.value.code == 1

    err = capsys.readouterr().err
    # Unmissable: a banner, not a grey line among a hundred others.
    assert "!!!!!!!!" in err
    assert "CORPUS SEEDING FAILED" in err
    # Each failed corpus is named ALONGSIDE what the demo loses without it, so
    # nobody has to re-derive whether it mattered at 7am.
    assert "fixture: NOT SEEDED" in err
    assert "Acme corpus" in err
    assert "csv: NOT SEEDED" in err
    assert "quarantine exhibit" in err
    # And it says how to retry just the seed, since the stack is already up.
    assert "ne connect sync" in err
    assert str(tmp_path / "connect" / "state.db") in err


def test_report_seed_failure_still_screams_for_an_unknown_source(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A source added later must not degrade the banner into silence."""
    with pytest.raises(SystemExit):
        live._report_seed_failure(("salesforce",), tmp_path)
    err = capsys.readouterr().err
    assert "salesforce: NOT SEEDED" in err
    assert "CORPUS SEEDING FAILED" in err


def test_every_source_the_host_seed_runs_has_declared_stakes() -> None:
    """Tripwire: the seed's source list and the banner's stakes map cannot drift.

    ``_seed_corpus`` pins ``NE_CONNECT_SYNC_SOURCES`` to exactly these two; if that
    list grows, the banner must learn what the new corpus is FOR rather than
    falling back to a generic line.
    """
    assert set(live._SEED_SOURCE_STAKES) == {"fixture", "csv"}


def test_seed_corpus_catches_the_named_failure_and_hands_it_to_demo_up() -> None:
    """The wiring tripwire: nothing between the sync and the banner may swallow.

    ``_seed_corpus`` composes secrets, docker paths and a full platform, so driving
    it end to end here would test the mocks. What actually has to hold is the
    control flow: ``_seed_corpus`` traps ``ConnectorSyncFailed`` and RETURNS its
    named sources, and ``demo_up`` routes a non-empty result into
    ``_report_seed_failure`` rather than logging and continuing.
    """
    source = inspect.getsource(live._seed_corpus)
    assert "except ConnectorSyncFailed as exc:" in source
    assert "return exc.sources" in source

    up = inspect.getsource(live.demo_up)
    assert "failed_sources = _seed_corpus(" in up
    assert "if failed_sources:" in up
    assert "_report_seed_failure(failed_sources, workdir)" in up
    # And the success line is still only reachable when nothing failed.
    assert up.index("_report_seed_failure(") < up.index("demo stack up; corpus seeded")


def test_seed_corpus_is_declared_to_return_the_failed_sources() -> None:
    assert inspect.signature(live._seed_corpus).return_annotation == "tuple[str, ...]"
