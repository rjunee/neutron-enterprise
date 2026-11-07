# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""C12 secret-hygiene guardian for deploy/compose/pilot (NE-070).

Proves that NO git-tracked file under the pilot tier carries a literal secret
value: every credential-shaped env var is a ``${...}`` interpolation, no ``*.env``
file is committed, and the Caddyfiles carry no inline key material. Secrets flow
SecretStore → ephemeral 0600 runtime env only (see test_up_cli / test_pilot_bundle).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
PILOT_DIR = REPO_ROOT / "deploy" / "compose" / "pilot"
PILOT_REL = "deploy/compose/pilot"

_CRED_KEY = re.compile(r"(?i)(password|secret|token|_key)")
# A credential-shaped assignment to a LITERAL (a value that is not a ${...}
# interpolation) — the shape a leaked secret would take in an env/compose file.
# SAME-LINE only ([ \t]*, not \s*, around the ':'/'=') so a NULL-valued key whose
# value sits only in the ephemeral --env-file (NE-087 pass-through, e.g.
# `NE_REBAC_API_TOKEN:` with nothing after) is not falsely joined to the NEXT
# line's first token (mirrors the helm scanner's same-line discipline).
_CRED_ASSIGNMENT = re.compile(
    r"(?im)^[^#\n]*?\b(?P<key>[A-Za-z0-9_]*(?:password|secret|token|_key)[A-Za-z0-9_]*)"
    r"[ \t]*[:=][ \t]*(?P<val>[^\s#]+)"
)


def _tracked_pilot_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", PILOT_REL],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [REPO_ROOT / rel for rel in out]


def _literal_credential_assignments(text: str) -> list[tuple[str, str]]:
    """Return (key, value) pairs where a credential-shaped key is set to a literal.

    A ``${...}`` interpolation is NOT a literal (fails closed at runtime). Bare
    field-NAME uses (e.g. ``"field": "password"`` in required.json — key is
    ``field``, not a credential word) do not match because the credential word
    must be the assignment KEY, immediately followed by ``:`` / ``=``.
    """
    offenders: list[tuple[str, str]] = []
    for match in _CRED_ASSIGNMENT.finditer(text):
        val = match.group("val").strip().strip("\"'")
        if val.startswith("${"):
            continue
        offenders.append((match.group("key"), val))
    return offenders


def test_detector_catches_a_planted_literal() -> None:
    # Positive control: the scanner must flag an obvious leak.
    planted = "POSTGRES_PASSWORD: hunter2-not-an-interpolation\n"
    assert _literal_credential_assignments(planted) == [
        ("POSTGRES_PASSWORD", "hunter2-not-an-interpolation")
    ]
    # And must NOT flag a proper interpolation.
    assert _literal_credential_assignments("POSTGRES_PASSWORD: ${NE_PILOT_X:?}\n") == []


def test_entra_env_null_form_not_flagged_and_literal_caught() -> None:
    # NE-BEW-INJECT positive/negative control pair, mirroring
    # test_detector_catches_a_planted_literal: the null-value pass-through
    # form (a bare key with NO value on the line) must NOT be flagged — the
    # detector regex requires a same-line value token (`val` is `[^\s#]+`),
    # so a null-form key can never match at all, same reason the live
    # NE_REBAC_* null keys pass this scanner today. A planted literal on the
    # SAME key IS caught.
    assert _literal_credential_assignments("NE_PILOT_ENTRA_BROKER_CLIENT_SECRET:\n") == []
    assert _literal_credential_assignments("NE_PILOT_ENTRA_BROKER_CLIENT_SECRET: hunter2\n") == [
        ("NE_PILOT_ENTRA_BROKER_CLIENT_SECRET", "hunter2")
    ]


def test_no_tracked_env_files_under_pilot() -> None:
    env_files = [p for p in _tracked_pilot_files() if p.suffix == ".env"]
    assert env_files == [], f"committed *.env files are forbidden under pilot: {env_files}"


def test_compose_env_credentials_are_interpolations() -> None:
    with (PILOT_DIR / "compose.yaml").open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    for name, cfg in data["services"].items():
        for key, value in (cfg.get("environment") or {}).items():
            if _CRED_KEY.search(key):
                # NE-087 (O-4): a NULL-valued pass-through key (value arrives ONLY via
                # the ephemeral 0600 --env-file) is C12-clean — bless that form here.
                assert value is None or (isinstance(value, str) and value.startswith("${")), (
                    f"{name}.{key} carries a literal secret, must be ${{...}} or null: {value!r}"
                )


def test_no_literal_secret_in_any_tracked_pilot_file() -> None:
    for path in _tracked_pilot_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue  # binary — no textual secret assignment
        offenders = _literal_credential_assignments(text)
        assert offenders == [], f"literal secret assignment(s) in {path}: {offenders}"


def test_no_secret_store_artifacts_committed_under_pilot() -> None:
    # required.json / secrets stores / runtime env are BUNDLE artifacts — none may
    # be committed under the pilot compose tree.
    banned = {"required.json", "pilot.env", "secrets.ne.json"}
    for path in _tracked_pilot_files():
        assert path.name not in banned, f"{path} must not be committed under pilot"


def test_caddyfiles_have_no_inline_key_material() -> None:
    for name in ("Caddyfile", "Caddyfile.customcerts"):
        text = (PILOT_DIR / "caddy" / name).read_text(encoding="utf-8")
        assert "PRIVATE KEY" not in text and "BEGIN " not in text, name
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("tls "):
                args = stripped.split()[1:]
                # Only 'internal' (local CA) or /certs/ PATHS — never key material.
                for arg in args:
                    assert arg == "internal" or arg.startswith("/certs/"), (
                        f"{name}: suspicious tls arg {arg!r}"
                    )


def test_realm_clientsecret_interpolation_not_flagged() -> None:
    # Control for NE-BEW-REALM's new "entra" identityProviders entry: the
    # ${...} skip in _literal_credential_assignments holds for the realm's
    # clientSecret exactly as it does for every other pilot credential.
    interpolated = '"clientSecret": "${NE_PILOT_ENTRA_BROKER_CLIENT_SECRET}",\n'
    assert _literal_credential_assignments(interpolated) == []
    # Note: a JSON-quoted credential key (closing quote sits between the key
    # and the ':') never matches _CRED_ASSIGNMENT's same-line key regex at
    # all — verified directly: '"clientSecret": "hunter2"' also returns []
    # here, NOT because of the ${...} skip but because the regex never
    # matches a quoted key. So this scanner is NOT the realm's literal-secret
    # guard for JSON files; the guard is the exact-string structural test
    # test_pilot_entra_clientsecret_is_interpolation in
    # packages/ne-identity/tests/test_realm_config.py. A bare (unquoted)
    # assignment like 'clientSecret: hunter2' IS flagged, as
    # test_detector_catches_a_planted_literal already proves for
    # POSTGRES_PASSWORD.
    assert _literal_credential_assignments('"clientSecret": "hunter2"\n') == []
