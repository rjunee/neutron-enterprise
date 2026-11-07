# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""Static guardian for the OFF-BY-DEFAULT CDC overlay (NE-084).

No docker daemon needed: the compose YAML is parsed with PyYAML and asserted
against the NE-084 contract — a STANDALONE `ne-cdc` project, external pilot
network, profile-gated, digest-pinned, no published ports, Standard-Webhooks
auth to the in-network ne-api sink, zero secret literals, full hardening. Also
proves the DEFAULT pilot compose ships NO Debezium/CDC service or env — the
off-by-default proof (belt-and-braces with the untouched exact-match matrix in
test_pilot_stack.py, which stays green because pilot/compose.yaml is untouched).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CDC_DIR = REPO_ROOT / "deploy" / "compose" / "cdc"
CDC_COMPOSE = CDC_DIR / "compose.yaml"
CDC_OVERRIDE = CDC_DIR / "pilot-override.yaml"
MERIDIAN_COMPOSE = CDC_DIR / "meridian-fee-postings.yaml"
MERIDIAN_PRESET = CDC_DIR / "meridian-fee-postings.env"
MERIDIAN_SEED = CDC_DIR / "meridian-fee-postings.sql"
MERIDIAN_INIT = CDC_DIR / "meridian-fee-postings-init.sh"
MERIDIAN_FIXTURE = REPO_ROOT / "tools" / "meridian" / "fixtures" / "meridian-v1" / "banking.json"
PILOT_COMPOSE = REPO_ROOT / "deploy" / "compose" / "pilot" / "compose.yaml"

_DEBEZIUM_PIN = re.compile(r"^quay\.io/debezium/server:3\.6\.0\.Final@sha256:[0-9a-f]{64}$")
_SINK_URL = "http://ne-api:8000/cdc/v1/events"
_CDC_ENV_KEYS = {
    "NE_API_CDC_WEBHOOK_SECRET",
    "NE_API_CDC_TABLES",
    "NE_API_CDC_QUARANTINE_PATH",
}


def _load(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _cdc() -> dict:
    return _load(CDC_COMPOSE)


def _debezium() -> dict:
    return _cdc()["services"]["debezium"]


def _env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return values


# --- off-by-default proof (the DEFAULT pilot compose) -----------------------


def test_default_pilot_compose_has_no_cdc_service_or_env() -> None:
    pilot = _load(PILOT_COMPOSE)
    services = pilot["services"]
    for name, cfg in services.items():
        assert "debezium" not in name and "cdc" not in name, f"pilot ships a CDC service: {name}"
        for key in cfg.get("environment") or {}:
            assert not key.startswith("NE_API_CDC_"), f"pilot {name} carries CDC env {key}"


# --- the standalone overlay -------------------------------------------------


def test_cdc_overlay_separate_project_named_ne_cdc() -> None:
    assert _cdc()["name"] == "ne-cdc"


def test_cdc_overlay_joins_pilot_network_external() -> None:
    net = _cdc()["networks"]["default"]
    assert net["name"] == "ne-pilot_default"
    assert net["external"] is True


def test_cdc_overlay_profile_gated() -> None:
    # Neither profile is a Compose default. The named Meridian profile is
    # deliberately exact; broad names such as `demo` would activate CDC for
    # unrelated demo deployments.
    assert _debezium().get("profiles") == ["cdc", "meridian-fee-postings"]


def test_cdc_debezium_image_digest_pinned() -> None:
    assert _DEBEZIUM_PIN.match(_debezium()["image"]), _debezium()["image"]


def test_cdc_publishes_no_ports() -> None:
    for name, cfg in _cdc()["services"].items():
        assert not cfg.get("ports"), f"{name} publishes ports (must be outbound-only)"


def test_cdc_sink_url_in_network_and_standard_webhooks_auth() -> None:
    env = _debezium()["environment"]
    assert env["DEBEZIUM_SINK_HTTP_URL"] == _SINK_URL
    assert env["DEBEZIUM_SINK_TYPE"] == "http"
    assert env["DEBEZIUM_SINK_HTTP_AUTHENTICATION_TYPE"] == "standard-webhooks"
    secret = env["DEBEZIUM_SINK_HTTP_AUTHENTICATION_WEBHOOK_SECRET"]
    assert secret.startswith("${NE_CDC_WEBHOOK_SECRET:?")


def test_cdc_no_secret_literals() -> None:
    # NB: narrower than the pilot guardian's `_key` clause — DEBEZIUM_FORMAT_KEY is
    # a serialization-format selector ("json"), not a credential.
    cred = re.compile(r"(?i)(password|secret|token)")
    for name, cfg in _cdc()["services"].items():
        for key, value in (cfg.get("environment") or {}).items():
            if cred.search(key):
                assert isinstance(value, str) and value.startswith("${"), (
                    f"{name}.{key} must be a ${{...}} interpolation, got {value!r}"
                )


def test_cdc_hardened() -> None:
    deb = _debezium()
    assert deb.get("security_opt") == ["no-new-privileges:true"]
    assert deb.get("cap_drop") == ["ALL"]
    assert deb.get("restart") == "unless-stopped"
    limits = deb["deploy"]["resources"]["limits"]
    assert "memory" in limits and "cpus" in limits
    assert deb["healthcheck"]["test"][0] == "CMD"
    assert deb["environment"]["OTEL_SDK_DISABLED"] == "true"


def test_cdc_source_db_credentials_are_interpolations() -> None:
    env = _debezium()["environment"]
    # Every source-DB credential is a ${...} interpolation, not a literal.
    for key in (
        "DEBEZIUM_SOURCE_DATABASE_HOSTNAME",
        "DEBEZIUM_SOURCE_DATABASE_USER",
        "DEBEZIUM_SOURCE_DATABASE_PASSWORD",
        "DEBEZIUM_SOURCE_DATABASE_DBNAME",
    ):
        assert str(env[key]).startswith("${"), key


# --- the pilot opt-in override ----------------------------------------------


def test_pilot_override_adds_identical_cdc_env_to_api_and_worker() -> None:
    override = _load(CDC_OVERRIDE)
    api_env = override["services"]["ne-api"]["environment"]
    worker_env = override["services"]["ne-worker"]["environment"]
    assert _CDC_ENV_KEYS.issubset(set(api_env))
    assert _CDC_ENV_KEYS.issubset(set(worker_env))
    # IDENTICAL values on both sides (config_hash parity).
    for key in _CDC_ENV_KEYS:
        assert api_env[key] == worker_env[key], key


# --- BD-12a: the exact-scope Meridian demo preset ---------------------------


def test_meridian_preset_captures_exactly_fee_postings() -> None:
    env = _env_file(MERIDIAN_PRESET)
    assert env["NE_CDC_TABLE_INCLUDE_LIST"] == "public.fee_postings"
    assert env["NE_CDC_SOURCE_DB_HOST"] == "meridian-fee-db"
    assert env["NE_CDC_SOURCE_DB_USER"] == "meridian_cdc"
    assert env["NE_CDC_SOURCE_DB_NAME"] == "meridian"

    sink = json.loads(env["NE_CDC_TABLES"])
    assert sink["source_name"] == "meridian-fee-postings"
    assert set(sink["tables"]) == {"public.fee_postings"}
    table = sink["tables"]["public.fee_postings"]
    assert table == {
        "key_columns": ["fee_posting_id"],
        "content_columns": [
            "fee_code",
            "amount_cents",
            "posted_date",
            "reversed",
            "customer_id",
            "deposit_account_id",
        ],
        "acl": {"principals": ["acl:tenant-all"]},
        "pii_class": "personal",
    }


def test_meridian_committed_preset_contains_no_secret_values_or_slots() -> None:
    env = _env_file(MERIDIAN_PRESET)
    secret_shaped = re.compile(r"(?i)(password|secret|token)")
    assert not {key for key in env if secret_shaped.search(key)}
    assert "whsec_" not in MERIDIAN_PRESET.read_text(encoding="utf-8")


def test_meridian_source_keeps_distinct_third_party_pin_and_is_profile_gated() -> None:
    pilot_postgres = _load(PILOT_COMPOSE)["services"]["postgres"]
    source = _load(MERIDIAN_COMPOSE)["services"]["meridian-fee-db"]
    assert source["image"] == (
        "postgres:18.6-alpine@"
        "sha256:77f585114c32fbca283dc835b0596f4e52b51b4c6662d7810b2f4084f60a1873"
    )
    assert source["image"] != pilot_postgres["image"]
    assert source["profiles"] == ["meridian-fee-postings"]
    assert not source.get("ports")
    assert source["security_opt"] == ["no-new-privileges:true"]
    assert source["restart"] == "unless-stopped"
    assert set(source["deploy"]["resources"]["limits"]) == {"memory", "cpus"}
    assert source["healthcheck"]["test"] == [
        "CMD-SHELL",
        "pg_isready -U meridian_owner -d meridian",
    ]
    command = source["command"]
    assert "wal_level=logical" in command
    assert "max_wal_senders=4" in command
    assert "max_replication_slots=4" in command
    assert source["environment"]["POSTGRES_PASSWORD"].startswith(
        "${NE_CDC_DEMO_DB_ADMIN_PASSWORD:?"
    )
    assert source["environment"]["NE_CDC_SOURCE_DB_PASSWORD"].startswith(
        "${NE_CDC_SOURCE_DB_PASSWORD:?"
    )


def test_meridian_debezium_cannot_broaden_its_publication() -> None:
    services = _load(MERIDIAN_COMPOSE)["services"]
    deb = services["debezium"]
    assert deb["depends_on"] == {"meridian-fee-db": {"condition": "service_healthy"}}
    assert deb["environment"] == {
        "DEBEZIUM_SOURCE_PUBLICATION_NAME": "meridian_fee_postings",
        "DEBEZIUM_SOURCE_SLOT_NAME": "meridian_fee_postings",
        "DEBEZIUM_SOURCE_PUBLICATION_AUTOCREATE_MODE": "disabled",
    }


_SEED_ROW = re.compile(
    r"\(\s*'(?P<fee_posting_id>mer-fee-\d+)'\s*,\s*"
    r"'(?P<customer_id>mer-cus-\d+)'\s*,\s*"
    r"'(?P<deposit_account_id>mer-dep-\d+)'\s*,\s*"
    r"'(?P<fee_code>[^']+)'\s*,\s*"
    r"(?P<amount_cents>\d+)\s*,\s*DATE\s+'(?P<posted_date>\d{4}-\d{2}-\d{2})'\s*,\s*"
    r"(?P<reversed>TRUE|FALSE)\s*\)"
)


def test_meridian_seed_sql_matches_the_canonical_fixture() -> None:
    sql = MERIDIAN_SEED.read_text(encoding="utf-8")
    actual: list[dict[str, object]] = []
    for match in _SEED_ROW.finditer(sql):
        row: dict[str, object] = {key: value for key, value in match.groupdict().items()}
        row["amount_cents"] = int(str(row["amount_cents"]))
        row["reversed"] = row["reversed"] == "TRUE"
        actual.append(row)
    canonical = json.loads(MERIDIAN_FIXTURE.read_text(encoding="utf-8"))["fee_postings"]
    assert actual == canonical


def test_meridian_sql_grants_only_the_named_table_and_publication() -> None:
    sql = MERIDIAN_SEED.read_text(encoding="utf-8")
    assert "GRANT SELECT ON TABLE public.fee_postings TO meridian_cdc;" in sql
    assert "CREATE PUBLICATION meridian_fee_postings FOR TABLE public.fee_postings" in sql
    assert "FOR ALL TABLES" not in sql.upper()
    assert sql.count("GRANT SELECT ON TABLE") == 1


def test_meridian_replication_password_never_rides_process_argv() -> None:
    script = MERIDIAN_INIT.read_text(encoding="utf-8")
    assert "--set=cdc_password" not in script
    assert "<<SQL" in script
    assert "*[!0-9a-f]*" in script
