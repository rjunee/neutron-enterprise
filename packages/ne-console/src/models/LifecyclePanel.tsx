// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError } from "../api/client";
import type {
  InferenceLifecycleAction,
  InferenceLifecycleTargetView,
  InferenceLifecycleView,
} from "../api/types";
import { useApiClient } from "../auth/useApiClient";
import { useGovernedRead } from "../landing/useGovernedRead";
import { StatePill } from "../primitives/StatePill";
import { LoadError, PendingMarker, PolicyDenied, Refused, SaveError } from "../primitives/states";
import { PrimaryButton, ToolbarButton } from "../primitives/Toolbar";
import { absoluteTime } from "../primitives/format";
import { desiredStateFor, lifecycleRows, lifecycleStateWord } from "./lifecycle";

const POLL_MS = 5_000;

type Attempt = {
  version: number;
  action: InferenceLifecycleAction;
  idempotencyKey: string;
  confirmationToken: string | null;
  settled: "success" | "failure" | null;
  needsFreshRead: boolean;
  evidenceAtSettlement: InferenceLifecycleView | null;
};

type AttemptView = Pick<
  Attempt,
  "version" | "action" | "settled" | "needsFreshRead" | "evidenceAtSettlement"
>;

type WriteState =
  | { kind: "idle" }
  | { kind: "confirming-stop" }
  | { kind: "saving" }
  | { kind: "refreshing" }
  /** Every `reason` below is the SERVER'S OWN served sentence, or null where it
   * sent none. Never a sentence the console composed: all three render as the
   * server's account of the outcome. */
  | { kind: "denied"; reason: string | null }
  | { kind: "refused"; reason: string | null }
  | { kind: "unavailable"; reason: string | null }
  | { kind: "error"; detail: string | null };

function randomIdempotencyKey(): string {
  return `console-${crypto.randomUUID()}`;
}

function initialWriteState(): WriteState {
  return { kind: "idle" };
}

function writeKey(
  target: InferenceLifecycleTargetView,
  action: InferenceLifecycleAction | "none",
): string {
  return `${target.target_id}:${target.version}:${action}`;
}

export function LifecyclePanel() {
  const client = useApiClient();
  const read = useGovernedRead<InferenceLifecycleView>(
    useCallback(() => client.modelLifecycle(), [client]),
  );
  const { reload } = read;
  const attempts = useRef(new Map<string, Attempt>());
  const [attemptViews, setAttemptViews] = useState<Record<string, AttemptView>>({});
  const [writes, setWrites] = useState<Record<string, WriteState>>({});

  useEffect(() => {
    const interval = window.setInterval(reload, POLL_MS);
    return () => window.clearInterval(interval);
  }, [reload]);

  const setWrite = useCallback((targetId: string, state: WriteState) => {
    setWrites((current) => ({ ...current, [targetId]: state }));
  }, []);

  const setAttemptView = useCallback((targetId: string, attempt: Attempt) => {
    setAttemptViews((current) => ({
      ...current,
      [targetId]: {
        version: attempt.version,
        action: attempt.action,
        settled: attempt.settled,
        needsFreshRead: attempt.needsFreshRead,
        evidenceAtSettlement: attempt.evidenceAtSettlement,
      },
    }));
  }, []);

  const execute = useCallback(
    async (target: InferenceLifecycleTargetView, action: InferenceLifecycleAction) => {
      const targetWriteKey = writeKey(target, action);
      const existing = attempts.current.get(target.target_id);
      const matchingExisting =
        existing?.version === target.version && existing.action === action ? existing : undefined;
      if (matchingExisting?.settled === "success") return;
      if (matchingExisting?.needsFreshRead) {
        if (matchingExisting.evidenceAtSettlement === read.data) return;
        matchingExisting.needsFreshRead = false;
        setAttemptView(target.target_id, matchingExisting);
      }
      const attempt = matchingExisting ?? {
        version: target.version,
        action,
        idempotencyKey: randomIdempotencyKey(),
        confirmationToken: null,
        settled: null,
        needsFreshRead: false,
        evidenceAtSettlement: null,
      };
      attempts.current.set(target.target_id, attempt);
      attempt.settled = null;
      setAttemptView(target.target_id, attempt);
      setWrite(targetWriteKey, { kind: "saving" });
      try {
        let token = attempt.confirmationToken;
        if (action === "stop" && token === null) {
          const confirmation = await client.confirmModelStop(target.target_id, target.version);
          token = confirmation.confirmation_token;
          attempt.confirmationToken = token;
        }
        await client.transitionModelTarget(
          target.target_id,
          desiredStateFor(action),
          target.version,
          attempt.idempotencyKey,
          token ?? undefined,
        );
        attempt.settled = "success";
        attempt.needsFreshRead = true;
        attempt.evidenceAtSettlement = read.data;
        setAttemptView(target.target_id, attempt);
        setWrite(targetWriteKey, { kind: "refreshing" });
      } catch (error: unknown) {
        attempt.settled = "failure";
        attempt.needsFreshRead = true;
        attempt.evidenceAtSettlement = read.data;
        setAttemptView(target.target_id, attempt);
        // `detail`, never `userMessage`: these three states render the string
        // as the SERVER'S reason, and `userMessage` is the console's own house
        // sentence whenever the server sent no `detail`. Null where it stated
        // nothing — the render below answers that in the console's own voice.
        if (error instanceof ApiError && error.status === 403) {
          setWrite(targetWriteKey, {
            kind: "denied",
            reason: error.detail ?? null,
          });
        } else if (error instanceof ApiError && (error.status === 409 || error.status === 404)) {
          setWrite(targetWriteKey, {
            kind: "refused",
            reason: error.detail ?? null,
          });
        } else if (error instanceof ApiError && error.status === 503) {
          setWrite(targetWriteKey, {
            kind: "unavailable",
            reason: error.detail ?? null,
          });
        } else {
          setWrite(targetWriteKey, {
            kind: "error",
            detail: error instanceof ApiError ? (error.detail ?? null) : null,
          });
        }
      } finally {
        // Applied, pending, and every failure resolve through fresh server
        // truth. No transition state is synthesized in this component.
        reload();
      }
    },
    [client, read.data, reload, setAttemptView, setWrite],
  );

  if (read.status === "refused") return <Refused thing="inference lifecycle" page="Models" />;
  if (read.status === "loading") return <p role="status">Loading inference targets…</p>;
  if (read.status === "error" || read.data === null) {
    return <LoadError things="inference targets" onRetry={read.reload} />;
  }

  const rows = lifecycleRows(read.data.targets);
  return (
    <section className="ne-models__lifecycle" aria-labelledby="ne-models-lifecycle-title">
      <div className="ne-models__lifecycleintro">
        <h2 id="ne-models-lifecycle-title" className="ne-models__servingtitle">
          Inference targets
        </h2>
        <p className="ne-models__servingcontext">
          Start and stop actions are offered only when the server reports them for the
          target&rsquo;s current version.
        </p>
      </div>
      {!read.data.transport_available ? (
        <p className="ne-models__routingnote" role="status">
          The inference operator is unavailable. Target status remains read-only.
        </p>
      ) : null}
      {rows.length === 0 ? (
        <p className="ne-models__routingnote">No inference targets are configured.</p>
      ) : (
        <ul className="ne-models__lifecyclelist">
          {rows.map((target) => {
            const attempt = attemptViews[target.target_id];
            const currentAttempt =
              attempt !== undefined &&
              attempt.version === target.version &&
              target.pending_change === null &&
              target.available_actions.includes(attempt.action)
                ? attempt
                : null;
            const retryReady =
              currentAttempt?.settled === "failure" &&
              currentAttempt.needsFreshRead &&
              currentAttempt.evidenceAtSettlement !== read.data;
            const rowAction = currentAttempt?.action ?? target.available_actions[0] ?? "none";
            const targetWriteKey = writeKey(target, rowAction);
            return (
              <LifecycleRow
                key={target.target_id}
                target={target}
                write={writes[targetWriteKey] ?? initialWriteState()}
                retryReady={retryReady}
                onWrite={(state) => setWrite(targetWriteKey, state)}
                onExecute={execute}
              />
            );
          })}
        </ul>
      )}
    </section>
  );
}

function LifecycleRow({
  target,
  write,
  retryReady,
  onWrite,
  onExecute,
}: {
  target: InferenceLifecycleTargetView;
  write: WriteState;
  retryReady: boolean;
  onWrite: (state: WriteState) => void;
  onExecute: (
    target: InferenceLifecycleTargetView,
    action: InferenceLifecycleAction,
  ) => Promise<void>;
}) {
  const retryBlocked =
    (write.kind === "denied" ||
      write.kind === "refused" ||
      write.kind === "unavailable" ||
      write.kind === "error") &&
    !retryReady;
  const saving = write.kind === "saving" || write.kind === "refreshing" || retryBlocked;
  const actions = useMemo(() => new Set(target.available_actions), [target.available_actions]);
  return (
    <li className="ne-models__lifecyclerow" data-target-id={target.target_id}>
      <div className="ne-models__lifecyclehead">
        <div>
          <strong>{target.display_name}</strong>
          <span className="ne-models__sub ne-mono"> {target.target_id}</span>
        </div>
        <StatePill word={lifecycleStateWord(target.state)} />
      </div>
      <dl className="ne-models__lifecyclefacts">
        <dt>Aliases</dt>
        <dd className="ne-mono">{target.aliases.length === 0 ? "—" : target.aliases.join(", ")}</dd>
        <dt>Code</dt>
        <dd>{target.code}</dd>
        <dt>Updated</dt>
        <dd>{absoluteTime(target.updated_at)}</dd>
        {target.cooldown_until === null ? null : (
          <>
            <dt>Cooldown until</dt>
            <dd>{absoluteTime(target.cooldown_until)}</dd>
          </>
        )}
      </dl>

      {target.pending_change === null ? null : (
        <PendingMarker
          by={target.pending_change.requested_by}
          requestId={target.pending_change.request_id}
        />
      )}

      {target.pending_change === null ? (
        <div className="ne-models__lifecycleactions">
          {actions.has("start") ? (
            <PrimaryButton
              label="Start"
              disabled={saving}
              onClick={() => void onExecute(target, "start")}
            />
          ) : null}
          {actions.has("stop") && write.kind !== "confirming-stop" ? (
            <ToolbarButton
              label="Stop"
              disabled={saving}
              onClick={() => onWrite({ kind: "confirming-stop" })}
            />
          ) : null}
          {actions.has("stop") && write.kind === "confirming-stop" ? (
            <span className="ne-models__lifecycleconfirm">
              <PrimaryButton label="Confirm stop" onClick={() => void onExecute(target, "stop")} />
              <ToolbarButton label="Cancel" onClick={() => onWrite({ kind: "idle" })} />
            </span>
          ) : null}
        </div>
      ) : null}

      {write.kind === "refreshing" || retryBlocked ? (
        <p className="ne-models__routingnote" role="status">
          Checking current target state…
        </p>
      ) : null}

      {write.kind === "denied" ? <PolicyDenied reason={write.reason} /> : null}
      {write.kind === "refused" || write.kind === "unavailable" ? (
        <p className="ne-models__refusal" role="alert">
          {/* The server's own words when it sent them; the console's own line,
              marked as the console's, when it did not. An empty alert is not an
              option and an invented reason is worse than none. */}
          {write.reason ?? "The transition didn’t happen; the server stated no reason."}
        </p>
      ) : null}
      {write.kind === "error" ? <SaveError detail={write.detail} /> : null}
    </li>
  );
}
