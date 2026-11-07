// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// FILL OUT SOME FIELDS, PRESS RUN.
//
// That is the owner's sentence, and the two-step version — "New conversation",
// land on another route, THEN fill out the form — is the detour he was
// complaining about. So Run does both: it mints the principal-owned
// Conversation the backend requires as a Run's container, then submits the Run
// into it.
//
// THE COST OF ONE CLICK IS AN HONEST FAILURE MODE, AND IT IS PAID IN FULL HERE.
// Two writes behind one button can half-succeed: the Conversation is created
// and the Run is not. The silent version of that leaves an empty Conversation
// in the principal's list with no explanation and a button that looks like it
// did nothing. So the write state below distinguishes the two failures by name,
// and the half-succeeded one SAYS the Conversation exists, links to it, and
// retries the byte-identical Run request into that same Conversation rather
// than creating a second one. The success receipt names both steps for the same
// reason: a reader who is told one thing happened should not later discover two
// did.
//
// The frozen request is frozen ACROSS retries — same body, same idempotency
// key, same `expected_version` — because the server's idempotency is what makes
// the retry safe, and regenerating either would forfeit it.

import { useCallback, useState } from "react";
import { Link } from "react-router";
import { Empty, LoadError, Loading, SaveError } from "../primitives/states";
import { capabilityMismatch, exactHash } from "./capabilityGuard";
import { RunForm } from "./RunForm";
import type {
  AcceptedAgentRunSubmission,
  AgentEligibility,
  AgentsClient,
  ConversationSummary,
  StartAgentRunRequest,
} from "./types";
import { useAgentsRead } from "./useAgentsRead";

type ConfigurationState = "loading" | "ready" | "unavailable";

/** THE STALE-ELIGIBILITY FAIL-CLOSED, KEPT.
 *
 * A Run is admitted against an exact agent version, definition hash and
 * capability snapshot. If any of them moved between the eligibility read and
 * the click, the server answers the SAME sanitized 404 it uses for a hidden
 * Agent — so a 404 on a submit is not "gone", it is "the answer you were
 * holding is out of date". Rendering that as a generic save error tells the
 * reader something broke and offers them nothing to do about it; this names it
 * and offers the one action that resolves it. Carried forward from the deleted
 * AgentLauncher, which is where this guarantee used to live. */
export const ELIGIBILITY_STALE =
  "Agent eligibility changed on the server. Refresh before continuing.";

function statusOf(error: unknown): number | null {
  if (typeof error !== "object" || error === null || !("status" in error)) return null;
  const status = (error as { status?: unknown }).status;
  return typeof status === "number" ? status : null;
}

function isStale(error: unknown): boolean {
  const status = statusOf(error);
  return status === 404 || status === 409;
}

function detailOf(error: unknown): string | null {
  if (typeof error !== "object" || error === null || !("detail" in error)) return null;
  const detail = (error as { detail?: unknown }).detail;
  return typeof detail === "string" ? detail : null;
}

type WriteState =
  | { readonly phase: "idle" }
  | { readonly phase: "creating" }
  | { readonly phase: "create-failed"; readonly detail: string | null }
  | { readonly phase: "stale" }
  | {
      readonly phase: "submitting";
      readonly conversation: ConversationSummary;
      readonly body: StartAgentRunRequest;
      readonly idempotencyKey: string;
    }
  | {
      readonly phase: "run-failed";
      readonly conversation: ConversationSummary;
      readonly body: StartAgentRunRequest;
      readonly idempotencyKey: string;
      readonly detail: string | null;
    }
  | {
      readonly phase: "accepted";
      readonly conversation: ConversationSummary;
      readonly receipt: AcceptedAgentRunSubmission;
    };

export function DeterministicRunLauncher({
  client,
  agentId,
  identityKey,
  platformConfigHash,
  configurationState,
  onAccepted,
  createIdempotencyKey = () => crypto.randomUUID(),
}: {
  client: AgentsClient;
  agentId: string;
  identityKey: string;
  platformConfigHash: string | null;
  configurationState: ConfigurationState;
  /** The server accepted the Run. The workspace SELECTS it — it does not
   * navigate to it. "Open Run" used to be a link to /agents/:id/runs/:runId,
   * which is the page the owner said was confusing to land on; the receipt now
   * opens in the docked pane on this same screen. */
  onAccepted?: (runId: string) => void;
  createIdempotencyKey?: () => string;
}) {
  const load = useCallback(async (): Promise<AgentEligibility> => {
    const [agent, preflight, profile] = await Promise.all([
      client.getAgent(agentId),
      client.getAgentPreflight(agentId),
      client.getAgentWorkflowProfile(agentId).catch(() => null),
    ]);
    return { agent, preflight, profile };
  }, [agentId, client]);
  const [eligibility, reload] = useAgentsRead<AgentEligibility>(load, identityKey);
  const [write, setWrite] = useState<WriteState>({ phase: "idle" });

  const submitRun = useCallback(
    async (
      conversation: ConversationSummary,
      body: StartAgentRunRequest,
      idempotencyKey: string,
    ) => {
      setWrite({ phase: "submitting", conversation, body, idempotencyKey });
      try {
        const receipt = await client.startAgentRun(agentId, body, idempotencyKey);
        setWrite({ phase: "accepted", conversation, receipt });
        onAccepted?.(receipt.run_id);
      } catch (error: unknown) {
        if (isStale(error)) {
          setWrite({ phase: "stale" });
          return;
        }
        setWrite({
          phase: "run-failed",
          conversation,
          body,
          idempotencyKey,
          detail: detailOf(error),
        });
      }
    },
    [agentId, client, onAccepted],
  );

  if (eligibility.phase === "loading" || configurationState === "loading") {
    return <Loading things="Agent eligibility" />;
  }
  if (eligibility.phase === "unavailable") return <Empty message="This Agent isn’t available." />;
  if (eligibility.phase === "error") {
    return <LoadError things="Agent eligibility" onRetry={reload} />;
  }

  const { agent, preflight, profile } = eligibility.value;
  const capabilities = preflight.capabilities;
  const mismatch = capabilityMismatch(agent, preflight);
  const platformReady = configurationState === "ready" && exactHash(platformConfigHash);
  const profileMatches =
    profile !== null &&
    profile.agent_id === agent.agent_id &&
    profile.agent_version === agent.version &&
    profile.agent_config_hash === agent.config_hash &&
    profile.strategy === "deterministic";
  const strategyAuthorized = preflight.model.allowed_strategies.includes("deterministic");

  const blockedReason =
    mismatch !== null
      ? mismatch
      : !platformReady
        ? "platform configuration is not ready."
        : !strategyAuthorized
          ? "the server does not authorize deterministic execution for this Agent."
          : null;

  if (!profileMatches) {
    return (
      <p className="ne-agent-launcher__availability" role="status">
        Deterministic launch is unavailable because its workflow profile does not match this Agent.
      </p>
    );
  }

  const start = (input: string) => {
    if (blockedReason !== null || capabilities === null || platformConfigHash === null) return;
    setWrite({ phase: "creating" });
    void client.createAgentConversation(agentId).then(
      (conversation) => {
        const body: StartAgentRunRequest = {
          conversation_id: conversation.conversation_id,
          expected_version: conversation.last_sequence,
          agent_version: agent.version,
          strategy: "deterministic",
          config_hash: platformConfigHash,
          capability_snapshot_hash: capabilities.capability_hash,
          input,
          requested_model: null,
          requested_model_lane: null,
        };
        void submitRun(conversation, body, createIdempotencyKey());
      },
      (error: unknown) =>
        setWrite(
          isStale(error) ? { phase: "stale" } : { phase: "create-failed", detail: detailOf(error) },
        ),
    );
  };

  const busy = write.phase === "creating" || write.phase === "submitting";

  return (
    <>
      <RunForm
        agent={agent}
        profile={profile}
        onSubmit={start}
        busy={busy}
        disabled={write.phase === "accepted" || write.phase === "stale"}
        blockedReason={blockedReason}
        idPrefix={`detrun-${agentId}`}
      />
      <div className="ag-runform__write" aria-live="polite">
        {write.phase === "stale" ? (
          <>
            <p role="alert">{ELIGIBILITY_STALE}</p>
            <button
              type="button"
              className="ne-btn"
              onClick={() => {
                setWrite({ phase: "idle" });
                reload();
              }}
            >
              Refresh eligibility
            </button>
          </>
        ) : null}
        {write.phase === "create-failed" ? (
          <>
            <SaveError detail={write.detail} />
            <p role="status">No Conversation was created and nothing ran.</p>
          </>
        ) : null}
        {write.phase === "run-failed" ? (
          <>
            <SaveError detail={write.detail} />
            <p role="alert">
              The Conversation was created, but the Run was not submitted into it. It is empty until
              this Run starts.
            </p>
            <Link
              to={`/agents/${encodeURIComponent(agentId)}?conversation=${encodeURIComponent(write.conversation.conversation_id)}`}
            >
              Open {write.conversation.title}
            </Link>
            <button
              type="button"
              className="ne-btn ne-btn--quiet"
              onClick={() => void submitRun(write.conversation, write.body, write.idempotencyKey)}
            >
              Retry exact request
            </button>
          </>
        ) : null}
        {write.phase === "accepted" ? (
          <article className="ne-agent-accepted" aria-label="Accepted Run receipt">
            <strong>{write.receipt.status}</strong>
            <span>Conversation created · Run submitted</span>
            <span>Version {write.receipt.version}</span>
            {write.receipt.replayed ? <span>Idempotent replay</span> : null}
            <span>
              Run {write.receipt.run_id} · selected in this workspace; its receipt opens here.
            </span>
          </article>
        ) : null}
      </div>
    </>
  );
}
