# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Pilot activation pins carried by the shared Compose manifest.

Two activations reach both app roots through the ONE shared ``x-ne-app-env``
anchor, and each is proven the same way — per ROOT, because an anchor only helps
if both services actually inherit it:

* Tier-2 graph — the null-valued ``NE_BRAIN_GRAPH_ENABLED`` pass-through (the
  NE-087 ``NE_REBAC_*`` idiom), written into the ephemeral 0600 ``--env-file`` by
  ``ne up --graph``. Off unless asked for.
* PRE-010G default Ask — ``NE_API_DEFAULT_ASK_GENERATION``, pinned to the canonical
  generation only by the generated live-writeback demo overlay. Like the graph key
  it is a shared null-valued pass-through, because every base/no-egress bring-up must
  leave both API and worker dormant and must not install the database activation fence.

NOT PROVEN HERE, deliberately: that an ACTIVATED deployment admits graph
POPULATION only behind a bound current-source-authority provider. That gate lives
in the ``build_platform`` composition root and is owned elsewhere; nothing in this
file asserts, implies or works around it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
PILOT_DIR = REPO_ROOT / "deploy/compose/pilot"
COMPOSE = PILOT_DIR / "compose.yaml"
SERVE_COMPOSE = REPO_ROOT / "deploy/compose/serve/compose.yaml"
ACTIVATION = "NE_BRAIN_GRAPH_ENABLED"
_DEFAULT_ASK_PIN = "NE_API_DEFAULT_ASK_GENERATION"
#: The exact DSN the pilot pins the graph tier to, with the ONE interpolation the
#: pilot's C12 secret hygiene requires removed.
_ADMITTED_GRAPH_DSN = "postgresql://ne:@postgres:5432/neutron"
_PASSWORD_INTERPOLATION = "${NE_PILOT_POSTGRES_PASSWORD}"
_REQUIRED_INTERPOLATION = re.compile(r"\$\{([A-Z0-9_]+):\?")


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "compose", "version"], capture_output=True, text=True, check=False
    )
    return probe.returncode == 0


_needs_compose = pytest.mark.skipif(
    not _docker_ready(), reason="the docker compose CLI resolves the null-valued pass-through"
)


def _anchor() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["x-ne-app-env"]


def _required_env_file(tmp_path: Path, *, activated: bool, default_ask: bool = False) -> Path:
    """Every ``${VAR:?}`` compose requires, DERIVED from the two compose files.

    Deriving rather than listing keeps this from silently rendering a stale
    subset of the stack when a new required interpolation is added.
    """
    names: set[str] = set()
    for path in (COMPOSE, SERVE_COMPOSE):
        names |= set(_REQUIRED_INTERPOLATION.findall(path.read_text(encoding="utf-8")))
    assert "NE_PILOT_POSTGRES_PASSWORD" in names, "the derivation found no required interpolations"
    lines = [f"{name}=render-only-not-a-secret" for name in sorted(names)]
    if activated:
        lines.append(f"{ACTIVATION}=true")
    if default_ask:
        lines.append(f"{_DEFAULT_ASK_PIN}={_canonical_generation()}")
    out = tmp_path / f"graph-{activated}-ask-{default_ask}.env"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def _rendered_env(env_file: Path) -> dict[str, dict[str, str | None]]:
    """The FULL ne-api/ne-worker container env docker compose itself resolves."""
    child_env = {k: v for k, v in os.environ.items() if not k.startswith("NE_")}
    proc = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE),
            "--env-file",
            str(env_file),
            "--profile",
            "core",
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=True,
        env=child_env,
        cwd=PILOT_DIR,
    )
    services = json.loads(proc.stdout)["services"]
    return {name: services[name]["environment"] for name in ("ne-api", "ne-worker")}


def _rendered_graph_env(env_file: Path) -> dict[str, dict[str, str | None]]:
    """The ne-api/ne-worker ``NE_BRAIN_GRAPH_*`` env docker compose itself resolves."""
    child_env = {k: v for k, v in os.environ.items() if not k.startswith("NE_")}
    proc = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE),
            "--env-file",
            str(env_file),
            "--profile",
            "core",
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=True,
        env=child_env,
        cwd=PILOT_DIR,
    )
    services = json.loads(proc.stdout)["services"]
    return {
        name: {
            key: value
            for key, value in services[name]["environment"].items()
            if key.startswith("NE_BRAIN_GRAPH_")
        }
        for name in ("ne-api", "ne-worker")
    }


# --- the paired deployment controls ------------------------------------------
@_needs_compose
def test_a_bring_up_that_does_not_ask_resolves_no_tier_2_activation(tmp_path: Path) -> None:
    """GRAPH-OFF (negative control). An env file with no activation key leaves the
    declaration unset, so no container receives ``NE_BRAIN_GRAPH_ENABLED``."""
    rendered = _rendered_graph_env(_required_env_file(tmp_path, activated=False))
    for env in rendered.values():
        assert env[ACTIVATION] is None
    # §6.2/Q5: the two ne-app roots must freeze the SAME config_hash, so they must
    # not disagree about whether the tier exists.
    assert rendered["ne-api"] == rendered["ne-worker"]


@_needs_compose
def test_activation_flows_from_the_env_file_and_moves_exactly_one_key(tmp_path: Path) -> None:
    """GRAPH-ON (positive control), against the same compose file and the same
    render path as the negative above — the ONLY difference is the env file."""
    off = _rendered_graph_env(_required_env_file(tmp_path, activated=False))
    on = _rendered_graph_env(_required_env_file(tmp_path, activated=True))
    for name in ("ne-api", "ne-worker"):
        assert on[name][ACTIVATION] == "true"
        # Activation must not also move the database or the graph it addresses: an
        # operator who turns the tier on must get the SAME admitted graph.
        assert {k: v for k, v in on[name].items() if k != ACTIVATION} == {
            k: v for k, v in off[name].items() if k != ACTIVATION
        }
    assert on["ne-api"] == on["ne-worker"]


# --- what the real consumer does with each rendered shape --------------------
def test_brain_graph_settings_are_off_when_unset_on_when_true_and_refuse_an_empty_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rendered env decides the tier through ``BrainGraphSettings``, not prose.

    The empty-string leg matters because it is the one shape between the two
    controls: a declaration that reached a container as ``""`` must REFUSE the
    deployment rather than resolve to either answer on its own.
    """
    from pydantic import ValidationError

    from ne.brain_graph.config import BrainGraphSettings

    anchor = _anchor()
    base = {
        "NE_BRAIN_GRAPH_DATABASE_URL": anchor["NE_BRAIN_GRAPH_DATABASE_URL"],
        "NE_BRAIN_GRAPH_LEDGER_URL": anchor["NE_BRAIN_GRAPH_LEDGER_URL"],
        "NE_BRAIN_GRAPH_GRAPH_NAME": anchor["NE_BRAIN_GRAPH_GRAPH_NAME"],
    }
    for key in tuple(os.environ):
        if key.startswith("NE_BRAIN_GRAPH_"):
            monkeypatch.delenv(key)
    for key, value in base.items():
        monkeypatch.setenv(key, value)

    assert BrainGraphSettings().enabled is False
    monkeypatch.setenv(ACTIVATION, "true")
    assert BrainGraphSettings().enabled is True
    monkeypatch.setenv(ACTIVATION, "")
    with pytest.raises(ValidationError):
        BrainGraphSettings()


# --- LIVE: the activated render against the real admitted server -------------
_AGE_LIVE_ENV = (
    "NE_AGE_DSN",
    "NE_AGE_ADMISSION_RECEIPT",
    "NE_AGE_EXPECTED_IMAGE_DIGEST",
    "NE_AGE_EXPECTED_SOURCE_REVISION",
)
_missing_live = tuple(name for name in _AGE_LIVE_ENV if not os.environ.get(name))


@pytest.mark.skipif(
    bool(_missing_live),
    reason="LIVE-PARKED: activated-render acceptance requires " + ", ".join(_missing_live),
)
@_needs_compose
def test_an_activated_render_opens_the_admitted_graph(tmp_path: Path) -> None:
    """LIVE. The env an ACTIVATED bring-up renders, carried into the real
    ``BrainGraphSettings`` and the production AGE store factory, opens the graph on
    a server whose admitted identity is read back and matched, not asserted.

    The ONE substitution is the DSN's network LOCATION: the compose DSN names the
    in-network ``postgres`` service and this process runs outside that network. The
    admitted identity is not substituted — the image digest and source revision come
    from the admission receipt the sidecar wrote, and the system identifier is read
    from ``pg_control_system()`` on the live server.
    """
    import asyncio

    from ne.api.platform import _default_graph_store
    from ne.brain_graph import _pg
    from ne.brain_graph.age.store import AgeGraphStore
    from ne.brain_graph.config import BrainGraphSettings

    dsn = os.environ["NE_AGE_DSN"]
    receipt = json.loads(Path(os.environ["NE_AGE_ADMISSION_RECEIPT"]).read_text(encoding="utf-8"))
    assert receipt["image_digest"] == os.environ["NE_AGE_EXPECTED_IMAGE_DIGEST"]
    assert receipt["source_revision"] == os.environ["NE_AGE_EXPECTED_SOURCE_REVISION"]

    async def _identity() -> tuple[str, str]:
        conn = await _pg.connect(dsn)
        try:
            version = await _pg.execute_sql(
                conn, "SELECT extversion FROM pg_extension WHERE extname = 'age'"
            )
            identity = await _pg.execute_sql(
                conn, "SELECT system_identifier FROM pg_control_system()"
            )
        finally:
            await conn.close()
        return str(version[0][0]), str(identity[0][0])

    extension, system_identifier = asyncio.run(_identity())
    assert extension == "1.7.0"
    # The receipt the age-admission sidecar wrote names THIS server, so the graph
    # the activated deployment opens below is the admitted one.
    assert system_identifier == receipt["system_identifier"]

    on = _rendered_graph_env(_required_env_file(tmp_path, activated=True))["ne-api"]
    off = _rendered_graph_env(_required_env_file(tmp_path, activated=False))["ne-api"]
    assert on[ACTIVATION] == "true"

    def _settings(rendered: dict[str, str | None]) -> BrainGraphSettings:
        return BrainGraphSettings(
            enabled=rendered[ACTIVATION] == "true",
            database_url=dsn,
            ledger_url=str(rendered["NE_BRAIN_GRAPH_LEDGER_URL"]),
            graph_name=str(rendered["NE_BRAIN_GRAPH_GRAPH_NAME"]),
        )

    # GRAPH-OFF: the identical deployment, rendered without --graph, composes no tier.
    assert _settings(off).enabled is False

    # GRAPH-ON: the same render WITH --graph opens the admitted graph by the name the
    # admission sidecar created.
    activated = _settings(on)
    assert activated.enabled is True

    async def _graph_names() -> list[str]:
        conn = await _pg.connect(dsn)
        try:
            rows = await _pg.execute_sql(conn, "SELECT name FROM ag_catalog.ag_graph")
        finally:
            await conn.close()
        return sorted(str(row[0]) for row in rows)

    admitted_graphs = asyncio.run(_graph_names())
    # The ADMISSION SIDECAR created this graph, before any app process existed. The
    # activated deployment must ATTACH to it, not mint one of its own — an app that
    # creates its own graph would read an empty tier on an admitted, populated server.
    assert activated.graph_name in admitted_graphs

    store = _default_graph_store(activated)
    assert isinstance(store, AgeGraphStore)

    async def _read() -> None:
        try:
            await store.ensure_graph()
            await store.scan_nodes()
        finally:
            await store.close()

    asyncio.run(_read())
    assert asyncio.run(_graph_names()) == admitted_graphs


# --- activation cannot move the graph off the admitted substrate -------------
def test_activation_cannot_repoint_the_tier_off_the_admitted_substrate() -> None:
    """The graph DSN is a compose LITERAL apart from the C12 password reference.

    ``ne up`` scrubs ambient ``NE_BRAIN_GRAPH_*`` so an operator export cannot decide
    activation; that scrub is only sufficient because the host, port and database
    are not interpolations either. A defaultable interpolation anywhere in this DSN
    would let an activated tier address a database the age-admission gate never
    admitted.
    """
    anchor = _anchor()
    dsn = anchor["NE_BRAIN_GRAPH_DATABASE_URL"]
    ledger_url = anchor["NE_BRAIN_GRAPH_LEDGER_URL"]
    assert dsn.count(_PASSWORD_INTERPOLATION) == 1
    assert dsn.replace(_PASSWORD_INTERPOLATION, "") == _ADMITTED_GRAPH_DSN
    assert "$" not in dsn.replace(_PASSWORD_INTERPOLATION, "")
    assert ledger_url.count(_PASSWORD_INTERPOLATION) == 1
    assert ledger_url.replace(_PASSWORD_INTERPOLATION, "") == _ADMITTED_GRAPH_DSN.replace(
        "postgresql://", "postgresql+psycopg://", 1
    )
    assert ledger_url.replace("postgresql+psycopg://", "postgresql://", 1) == dsn
    assert "$" not in ledger_url.replace(_PASSWORD_INTERPOLATION, "")
    assert "$" not in str(anchor["NE_BRAIN_GRAPH_GRAPH_NAME"])
    # Positive control: this anchor DOES carry a defaultable interpolation on
    # another key, so the absence above is a real exclusion rather than a check
    # that could never fire.
    assert "${NE_VLLM_SERVED_NAME:-" in anchor["NE_GATEWAY_MODELS"]


# --- PRE-010G: the canonical default-Ask pin reaches BOTH roots ---------------


def _canonical_generation() -> str:
    """The generation the ADMITTING code uses, never a string retyped in a test."""
    from ne.api.default_ask_submission import DEFAULT_ASK_WIRE_GENERATION

    return DEFAULT_ASK_WIRE_GENERATION


def test_both_pilot_roots_share_one_null_valued_default_ask_pass_through() -> None:
    """Asserted PER ROOT. ne-api ADMITS the Ask and ne-worker EXECUTES the canonical
    Run, so a manifest only one root inherits yields a deployment that accepts work
    it cannot run — and that half-state is not loud anywhere else.

    ne-worker takes the anchor through a MERGE key, so it can carry a per-service
    override that shadows the shared value; this reads the merged per-service maps
    rather than the anchor alone.
    """
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    for service in ("ne-api", "ne-worker"):
        assert data["services"][service]["environment"][_DEFAULT_ASK_PIN] is None, service
    # ONE shared entry, so the two roots cannot drift apart in a later edit.
    assert data["x-ne-app-env"][_DEFAULT_ASK_PIN] is None
    # The companion half of the same readiness gate is retained and equally shared.
    assert data["x-ne-app-env"]["NE_AGENT_TRANSPORT_GENERATION"] == "agent-transport-v1"


@_needs_compose
def test_base_roots_are_dormant_and_shared_override_pins_them_together(tmp_path: Path) -> None:
    """The renderer proves the base-negative and shared-overlay-value mechanics."""
    dormant = _rendered_env(_required_env_file(tmp_path, activated=False))
    for service, env in dormant.items():
        assert env[_DEFAULT_ASK_PIN] is None, service

    rendered = _rendered_env(_required_env_file(tmp_path, activated=False, default_ask=True))
    canonical = _canonical_generation()
    for service, env in rendered.items():
        assert env[_DEFAULT_ASK_PIN] == canonical, service
    assert rendered["ne-api"][_DEFAULT_ASK_PIN] == rendered["ne-worker"][_DEFAULT_ASK_PIN]
    # Activation of the graph tier is orthogonal: the default-Ask pin is identical
    # whether or not `ne up --graph` was passed.
    activated = _rendered_env(_required_env_file(tmp_path, activated=True, default_ask=True))
    assert activated["ne-api"][_DEFAULT_ASK_PIN] == canonical
    assert activated["ne-worker"][_DEFAULT_ASK_PIN] == canonical


def test_the_pinned_generation_is_admissible_and_arbitrary_generations_are_not(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Derived from the SETTINGS TYPE, not from a string restated here.

    ``ApiSettings.default_ask_generation`` is a closed ``Literal``, so the admissible
    set is computed from the annotation itself: widening it, or drifting the manifest
    pin off it, fails here rather than at a deployment.

    The generations are supplied through the ENV, which is the shape a manifest
    actually produces — a wrong generation arrives as a string in a deployment's
    environment, never as a Python literal a type checker could have caught.
    """
    from typing import get_args

    from pydantic import ValidationError

    from ne.api.settings import ApiSettings

    anchor = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["x-ne-app-env"]
    admissible = set(get_args(ApiSettings.model_fields["default_ask_generation"].annotation))
    assert admissible == {"", _canonical_generation()}

    assert anchor[_DEFAULT_ASK_PIN] is None
    pinned = _canonical_generation()

    # The manifest's own required ApiSettings values, so every construction below is
    # the deployment's configuration rather than an invented one.
    required = {
        "qdrant_url": anchor["NE_API_QDRANT_URL"],
        "qdrant_collection": anchor["NE_API_QDRANT_COLLECTION"],
        "tei_url": anchor["NE_API_TEI_URL"],
    }
    monkeypatch.delenv(_DEFAULT_ASK_PIN, raising=False)
    assert ApiSettings(**required).default_ask_generation == ""

    monkeypatch.setenv(_DEFAULT_ASK_PIN, pinned)
    assert ApiSettings(**required).default_ask_generation == pinned

    monkeypatch.setenv(_DEFAULT_ASK_PIN, "default-ask/canonical-run/v2")
    with pytest.raises(ValidationError):
        ApiSettings(**required)
