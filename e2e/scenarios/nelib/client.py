# Copyright (c) 2026 Ryan Junee.
# SPDX-License-Identifier: Apache-2.0
"""HTTP driver for one live Neutron Enterprise deployment.

The suite authenticates as real Keycloak principals (``ops`` drives runs,
``approver`` decides approvals — separation of duties is part of what S5
proves) and speaks only the governed ``/api/v1`` surface. Every completed
scenario dumps its full transcript (run detail, events, evidence, turns) into
the results directory so a red run is diagnosable without re-running it.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

TERMINAL_STATUSES = frozenset(
    {
        "completed",
        "failed",
        "cancelled",
        "expired",
        "context_exhausted",
        "budget_exhausted",
        "unavailable",
    }
)

# Registered demo agents (deploy/agents/registry.toml).
MERIDIAN_ANALYST = "agt_01k3meridiananalyst000000000"
CUSTOMER_SUPPORT = "agt_01k3customersupport000000000"
GROUNDED_ANALYST = "agt_01k3groundedanalyst000000000"
OVERDUE_CREDIT_REVIEW = "agt_01k3overduecreditreview00000"


class SubmitRefused(Exception):
    """A run submission the API refused (HTTP error before any run existed)."""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"run submission refused: HTTP {status_code}: {body[:400]}")
        self.status_code = status_code
        self.body = body


@dataclass
class RunResult:
    """One live run: everything the governed surface serves about it."""

    run_id: str
    conversation_id: str
    elapsed_s: float
    detail: dict[str, Any]
    events: list[dict[str, Any]]
    evidence: dict[str, Any]

    @property
    def status(self) -> str:
        return str(self.detail["status"])

    @property
    def verdict(self) -> str | None:
        verdict = self.detail.get("output_verdict")
        return None if verdict is None else str(verdict)

    @property
    def failure_reason(self) -> str:
        return str(self.detail.get("failure_reason") or "")

    @property
    def served_model(self) -> str | None:
        model = self.detail.get("served_model")
        return None if model is None else str(model)

    @property
    def answer(self) -> str:
        """The served answer text (evidence projection first, preview fallback)."""
        output = self.evidence.get("output")
        if isinstance(output, dict) and isinstance(output.get("display"), str):
            return output["display"]
        preview = self.detail.get("output_preview")
        return preview if isinstance(preview, str) else ""

    @property
    def tool_calls(self) -> list[str]:
        """Tool names in execution order, from the run's own audit-intent events."""
        calls: list[str] = []
        for event in self.events:
            if event.get("event_kind") != "run.audit.intent":
                continue
            meta = {
                str(item.get("key")): str(item.get("value"))
                for item in event.get("metadata") or []
                if isinstance(item, dict)
            }
            if meta.get("audit_action") == "run.tool.pre_effect" and "tool_name" in meta:
                calls.append(meta["tool_name"])
        return calls


@dataclass
class NeutronClient:
    """One authenticated principal against one deployment."""

    api_base: str
    keycloak_base: str
    realm: str
    username: str
    password: str
    verify_tls: bool = True
    results_dir: Path | None = None
    poll_interval_s: float = 5.0
    _token: str | None = field(default=None, repr=False)
    _token_expires_at: float = field(default=0.0, repr=False)
    _http: httpx.Client | None = field(default=None, repr=False)

    def close(self) -> None:
        if self._http is not None:
            self._http.close()
            self._http = None

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(verify=self.verify_tls, timeout=90.0)
        return self._http

    # -- auth ------------------------------------------------------------

    def _fresh_token(self) -> str:
        response = self.http.post(
            f"{self.keycloak_base}/realms/{self.realm}/protocol/openid-connect/token",
            data={
                "grant_type": "password",
                "client_id": "ne-console",
                "scope": "openid",
                "username": self.username,
                "password": self.password,
            },
        )
        response.raise_for_status()
        payload = response.json()
        self._token = str(payload["access_token"])
        # Refresh two minutes early; the realm lifespan is ~900s and an expired
        # token mid-poll must never look like a scenario failure.
        self._token_expires_at = time.monotonic() + float(payload.get("expires_in", 300)) - 120
        return self._token

    def _auth_header(self) -> dict[str, str]:
        if self._token is None or time.monotonic() >= self._token_expires_at:
            self._fresh_token()
        assert self._token is not None
        return {"Authorization": f"Bearer {self._token}"}

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        url = f"{self.api_base}{path}"
        merged = {**self._auth_header(), **(headers or {})}
        response = self.http.request(method, url, json=json_body, params=params, headers=merged)
        if response.status_code == 401:
            self._fresh_token()
            merged = {**self._auth_header(), **(headers or {})}
            response = self.http.request(method, url, json=json_body, params=params, headers=merged)
        return response

    def get_json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        response = self.request("GET", path, params=params)
        response.raise_for_status()
        return response.json()

    # -- platform reads --------------------------------------------------

    def posture(self) -> dict[str, Any]:
        return self.get_json("/api/v1/posture")

    def connect_status(self) -> dict[str, Any]:
        return self.get_json("/api/v1/connect/status")

    def m365_connector(self) -> dict[str, Any] | None:
        connectors = self.connect_status().get("connectors", [])
        for connector in connectors:
            if connector.get("kind") == "m365":
                return connector
        return None

    def preflight(self, agent_id: str) -> dict[str, Any]:
        return self.get_json(f"/api/v1/agents/{agent_id}/preflight")

    # -- conversations + runs -------------------------------------------

    def create_conversation(self, agent_id: str) -> dict[str, Any]:
        response = self.request("POST", f"/api/v1/agents/{agent_id}/conversations", json_body={})
        response.raise_for_status()
        return response.json()

    def conversation(self, agent_id: str, conversation_id: str) -> dict[str, Any]:
        return self.get_json(f"/api/v1/agents/{agent_id}/conversations/{conversation_id}")

    def submit_run(
        self,
        agent_id: str,
        conversation_id: str,
        expected_version: int,
        text: str,
        *,
        requested_model: str | None = None,
    ) -> str:
        preflight = self.preflight(agent_id)
        posture = self.posture()
        body: dict[str, Any] = {
            "conversation_id": conversation_id,
            "expected_version": expected_version,
            "agent_version": preflight["version"],
            "strategy": "agentic",
            "config_hash": posture["config_hash"],
            "capability_snapshot_hash": preflight["capabilities"]["capability_hash"],
            "input": text,
        }
        if requested_model is not None:
            body["requested_model"] = requested_model
        response = self.request(
            "POST",
            f"/api/v1/agents/{agent_id}/runs",
            json_body=body,
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        if response.status_code >= 400:
            raise SubmitRefused(response.status_code, response.text)
        return str(response.json()["run_id"])

    def run_detail(self, agent_id: str, run_id: str) -> dict[str, Any]:
        return self.get_json(f"/api/v1/agents/{agent_id}/runs/{run_id}")

    def wait_for_status(
        self,
        agent_id: str,
        run_id: str,
        *,
        deadline_s: float,
        stop_statuses: frozenset[str] = TERMINAL_STATUSES,
    ) -> dict[str, Any]:
        """Poll until the run reaches one of ``stop_statuses`` or the deadline.

        Returns the LAST detail either way — the caller asserts on the status,
        so a hang is reported as the state it hung in, never as a poll crash.
        """
        deadline = time.monotonic() + deadline_s
        detail = self.run_detail(agent_id, run_id)
        while time.monotonic() < deadline:
            if detail["status"] in stop_statuses:
                return detail
            time.sleep(self.poll_interval_s)
            detail = self.run_detail(agent_id, run_id)
        return detail

    def run_events(self, agent_id: str, run_id: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            page = self.get_json(f"/api/v1/agents/{agent_id}/runs/{run_id}/events", params=params)
            events.extend(page["items"])
            cursor = page.get("next_cursor")
            if not cursor:
                return events

    def run_evidence(self, agent_id: str, run_id: str) -> dict[str, Any]:
        try:
            return self.get_json(f"/api/v1/agents/{agent_id}/runs/{run_id}/evidence")
        except httpx.HTTPStatusError as exc:
            return {"error": f"HTTP {exc.response.status_code}"}

    def collect(
        self, agent_id: str, run_id: str, conversation_id: str, elapsed_s: float
    ) -> RunResult:
        return RunResult(
            run_id=run_id,
            conversation_id=conversation_id,
            elapsed_s=elapsed_s,
            detail=self.run_detail(agent_id, run_id),
            events=self.run_events(agent_id, run_id),
            evidence=self.run_evidence(agent_id, run_id),
        )

    def run_turn(
        self,
        agent_id: str,
        text: str,
        *,
        requested_model: str | None = None,
        conversation_id: str | None = None,
        deadline_s: float = 600.0,
        label: str | None = None,
        stop_statuses: frozenset[str] = TERMINAL_STATUSES,
    ) -> RunResult:
        """Submit one turn, wait it out, collect and archive the transcript."""
        if conversation_id is None:
            conversation = self.create_conversation(agent_id)
            conversation_id = str(conversation["conversation_id"])
            expected_version = int(conversation["last_sequence"])
        else:
            conversation = self.conversation(agent_id, conversation_id)
            expected_version = int(conversation["last_sequence"])
        started = time.monotonic()
        run_id = self.submit_run(
            agent_id,
            conversation_id,
            expected_version,
            text,
            requested_model=requested_model,
        )
        self.wait_for_status(agent_id, run_id, deadline_s=deadline_s, stop_statuses=stop_statuses)
        result = self.collect(agent_id, run_id, conversation_id, time.monotonic() - started)
        if label is not None:
            self.archive(label, result)
        return result

    def archive(self, label: str, result: RunResult) -> None:
        if self.results_dir is None:
            return
        self.results_dir.mkdir(parents=True, exist_ok=True)
        path = self.results_dir / f"{label}.{result.run_id[:8]}.json"
        path.write_text(
            json.dumps(
                {
                    "label": label,
                    "run_id": result.run_id,
                    "conversation_id": result.conversation_id,
                    "elapsed_s": round(result.elapsed_s, 1),
                    "detail": result.detail,
                    "events": result.events,
                    "evidence": result.evidence,
                },
                indent=1,
                default=str,
            )
        )

    # -- approvals -------------------------------------------------------

    def pending_approvals(self) -> list[dict[str, Any]]:
        return list(self.get_json("/api/v1/approvals/pending").get("approvals", []))

    def approval_payload(self, request_id: str) -> httpx.Response:
        return self.request("GET", f"/api/v1/approvals/{request_id}/payload")

    def approve(self, request_id: str) -> httpx.Response:
        return self.request("POST", f"/api/v1/approvals/{request_id}/approve", json_body={})

    def approval_decisions(self, request_id: str) -> httpx.Response:
        return self.request("GET", f"/api/v1/audit/approval-decisions/{request_id}")

    # -- audit -----------------------------------------------------------

    def audit_events(self, *, action: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit}
        if action is not None:
            params["action"] = action
        return list(self.get_json("/api/v1/audit/events", params=params).get("events", []))

    # -- connector sync (S8) --------------------------------------------

    def request_sync(self, source: str) -> dict[str, Any]:
        response = self.request("POST", f"/api/v1/connect/{source}/sync", json_body={})
        response.raise_for_status()
        return response.json()

    def sync_status(self, request_id: str) -> dict[str, Any]:
        return self.get_json(f"/api/v1/connect/sync/{request_id}")

    # -- answer ledger (S8) ---------------------------------------------

    def ledger(self) -> dict[str, Any]:
        return self.get_json("/api/v1/answer-ledger")

    def ledger_pin(self, question: str) -> httpx.Response:
        return self.request("POST", "/api/v1/answer-ledger/pin", json_body={"question": question})

    def ledger_reask(self) -> dict[str, Any]:
        response = self.request("POST", "/api/v1/answer-ledger/reask")
        response.raise_for_status()
        return response.json()
