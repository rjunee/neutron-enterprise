// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { useEffect, useRef, useState } from "react";
import { Link, useNavigate } from "react-router";
import { ApiError } from "../api/client";
import type { PendingApprovalView } from "../api/types";
import { DecisionPane } from "../approvals/DecisionPane";
import type { DecisionState } from "../approvals/DecisionPane";
import type { AgentsClient } from "./types";

interface ApprovalReadState {
  client: AgentsClient;
  key: string;
  phase: "loading" | "ready" | "unavailable";
  approval: PendingApprovalView | null;
}

/** Inline view of the exact approval named by the current owner-scoped Run.
 * The queue is only an availability check: it is never searched by recency. */
export function RunApprovalPane({
  client,
  approvalId,
  identityKey,
  agentId,
  runId,
  onRefetchRun,
}: {
  client: AgentsClient;
  approvalId: string | null;
  identityKey: string;
  agentId: string;
  runId: string;
  onRefetchRun: () => void;
}) {
  const key = `${identityKey}\u0000${agentId}\u0000${runId}\u0000${approvalId ?? ""}`;
  const [read, setRead] = useState<ApprovalReadState>({
    client,
    key,
    phase: "loading",
    approval: null,
  });
  const [decision, setDecision] = useState<{
    client: AgentsClient;
    key: string;
    state: DecisionState;
  }>({
    client,
    key,
    state: {},
  });
  const [now] = useState(() => Date.now());
  const [closedKey, setClosedKey] = useState<string | null>(null);
  const inFlight = useRef<string | null>(null);

  useEffect(() => {
    let active = true;
    inFlight.current = null;
    if (approvalId === null || client.listPendingApprovals === undefined) {
      void Promise.resolve().then(() => {
        if (active) {
          setRead({ client, key, phase: "unavailable", approval: null });
          setDecision({ client, key, state: {} });
        }
      });
      return () => {
        active = false;
      };
    }
    void client.listPendingApprovals().then(
      ({ approvals }) => {
        if (!active) return;
        const matches = approvals.filter(({ request_id }) => request_id === approvalId);
        setRead({
          client,
          key,
          phase: matches.length === 1 ? "ready" : "unavailable",
          approval: matches.length === 1 ? matches[0]! : null,
        });
        setDecision({ client, key, state: {} });
      },
      () => {
        if (active) {
          setRead({ client, key, phase: "unavailable", approval: null });
          setDecision({ client, key, state: {} });
        }
      },
    );
    return () => {
      active = false;
    };
  }, [approvalId, agentId, client, identityKey, key, runId]);

  if (approvalId === null) return null;
  const currentRead =
    read.client === client && read.key === key
      ? read
      : { client, key, phase: "loading" as const, approval: null };
  const currentDecision = decision.client === client && decision.key === key ? decision.state : {};
  const decide = async (kind: "approve" | "reject", reason?: string) => {
    if (inFlight.current !== null) return;
    const action = kind === "approve" ? client.approve : client.reject;
    if (action === undefined) return;
    inFlight.current = key;
    setDecision({ client, key, state: { startedAt: Date.now() } });
    try {
      const result = await (kind === "approve"
        ? client.approve!(approvalId)
        : client.reject!(approvalId, reason));
      if (inFlight.current !== key) return;
      setDecision({ client, key, state: { result } });
      onRefetchRun();
    } catch (error) {
      if (inFlight.current !== key) return;
      const conflict = error instanceof ApiError && error.status === 409;
      setDecision({
        client,
        key,
        state: {
          error: {
            conflict,
            detail: error instanceof ApiError ? (error.detail ?? null) : null,
          },
        },
      });
      if (conflict) onRefetchRun();
    } finally {
      if (inFlight.current === key) inFlight.current = null;
    }
  };

  if (closedKey === key) {
    return <Link to="/approvals">Open approval queue</Link>;
  }
  if (
    currentRead.phase !== "ready" ||
    currentRead.approval === null ||
    client.approve === undefined ||
    client.reject === undefined
  ) {
    return (
      <section aria-label="Run approval">
        <p>
          {currentRead.phase === "loading"
            ? "Loading exact approval…"
            : "Inline approval unavailable."}
        </p>
        <Link to="/approvals">Open approval queue</Link>
      </section>
    );
  }
  return (
    <section aria-label="Run approval">
      <DecisionPane
        approval={currentRead.approval}
        now={now}
        state={currentDecision}
        decidedElsewhere={currentDecision.error?.conflict === true}
        auditSeq={null}
        chainReadable={false}
        onClose={() => setClosedKey(key)}
        onApprove={() => void decide("approve")}
        onDeny={(reason) => void decide("reject", reason)}
      />
      <Link to="/approvals">Open approval queue</Link>
    </section>
  );
}

/** Approver-scoped, content-free Run shell.
 *
 * This route deliberately never asks for the owner-scoped Run. Its only
 * authority is the exact queue row plus the governed payload disclosure's
 * closed run_context tuple, all bound to the current principal generation.
 */
export function ApprovalOnlyRunPane({
  client,
  approvalId,
  identityKey,
  agentId,
  runId,
}: {
  client: AgentsClient;
  approvalId: string;
  identityKey: string;
  agentId: string;
  runId: string;
}) {
  const navigate = useNavigate();
  const key = `${identityKey}\u0000${agentId}\u0000${runId}\u0000${approvalId}`;
  const [read, setRead] = useState<ApprovalReadState>({
    client,
    key,
    phase: "loading",
    approval: null,
  });
  const [decision, setDecision] = useState<{
    client: AgentsClient;
    key: string;
    state: DecisionState;
  }>({ client, key, state: {} });
  const [now] = useState(() => Date.now());
  const inFlight = useRef<string | null>(null);

  useEffect(() => {
    let active = true;
    inFlight.current = null;
    if (
      client.listPendingApprovals === undefined ||
      client.readApprovalPayload === undefined ||
      client.approve === undefined ||
      client.reject === undefined
    ) {
      void Promise.resolve().then(() => {
        if (active) {
          setRead({ client, key, phase: "unavailable", approval: null });
          setDecision({ client, key, state: {} });
        }
      });
      return () => {
        active = false;
      };
    }
    void Promise.all([client.listPendingApprovals(), client.readApprovalPayload(approvalId)]).then(
      ([{ approvals }, payload]) => {
        if (!active) return;
        const matches = approvals.filter(({ request_id }) => request_id === approvalId);
        const approval = matches.length === 1 ? matches[0]! : null;
        const available =
          payload.disclosure_status === "available" && "action" in payload ? payload : null;
        const context = available?.run_context;
        const exact =
          approval !== null &&
          payload.request_id === approvalId &&
          available !== null &&
          available.action === approval.action &&
          available.tier === approval.tier &&
          available.principal_id === approval.principal_id &&
          context !== undefined &&
          context.approval_id === approvalId &&
          context.agent_id === agentId &&
          context.run_id === runId;
        setRead({
          client,
          key,
          phase: exact ? "ready" : "unavailable",
          approval: exact ? approval : null,
        });
        setDecision({ client, key, state: {} });
      },
      () => {
        if (active) {
          setRead({ client, key, phase: "unavailable", approval: null });
          setDecision({ client, key, state: {} });
        }
      },
    );
    return () => {
      active = false;
    };
  }, [agentId, approvalId, client, identityKey, key, runId]);

  const currentRead =
    read.client === client && read.key === key
      ? read
      : { client, key, phase: "loading" as const, approval: null };
  const currentDecision = decision.client === client && decision.key === key ? decision.state : {};
  const decide = async (kind: "approve" | "reject", reason?: string) => {
    if (inFlight.current !== null || currentRead.phase !== "ready") return;
    inFlight.current = key;
    setDecision({ client, key, state: { startedAt: Date.now() } });
    try {
      const result = await (kind === "approve"
        ? client.approve!(approvalId)
        : client.reject!(approvalId, reason));
      if (inFlight.current === key) setDecision({ client, key, state: { result } });
    } catch (error) {
      if (inFlight.current !== key) return;
      setDecision({
        client,
        key,
        state: {
          error: {
            conflict: error instanceof ApiError && error.status === 409,
            detail: error instanceof ApiError ? (error.detail ?? null) : null,
          },
        },
      });
    } finally {
      if (inFlight.current === key) inFlight.current = null;
    }
  };

  if (currentRead.phase !== "ready" || currentRead.approval === null) {
    return (
      <section aria-label="Run approval">
        <p>
          {currentRead.phase === "loading"
            ? "Loading exact approval…"
            : "This approval is unavailable."}
        </p>
        <Link to={`/approvals?request=${encodeURIComponent(approvalId)}`}>Open approval queue</Link>
      </section>
    );
  }
  return (
    <section aria-label="Run approval">
      <DecisionPane
        approval={currentRead.approval}
        now={now}
        state={currentDecision}
        decidedElsewhere={currentDecision.error?.conflict === true}
        auditSeq={null}
        chainReadable={false}
        onClose={() => navigate(`/approvals?request=${encodeURIComponent(approvalId)}`)}
        onApprove={() => void decide("approve")}
        onDeny={(reason) => void decide("reject", reason)}
      />
      <Link to={`/approvals?request=${encodeURIComponent(approvalId)}`}>Open approval queue</Link>
    </section>
  );
}
