// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// NE-141 — the Dual-Key Compare pane + its state machine (design §5.3). It runs
// the IDENTICAL governed query bodies under EACH bearer (the operator's primary
// session client A, and the second-persona compare client B) and renders the two
// INDEPENDENTLY server-trimmed responses side by side — the client never diffs
// privileged data; it only shows what each key's server response contained (C7).
// The contraction overlay animates the REAL A∖B difference (§0 law 5). Every
// failure — popup already handled upstream, a key-B 401 (key expired) or a
// 403/404/5xx refusal — renders an HONEST placard; no fabricated trimmed view is
// ever shown, and no contraction runs without BOTH real graph responses. Exit =
// the second token is DISCARDED (KEY WITHDRAWN), including on unmount/navigation.

import { useEffect, useMemo, useRef, useState } from "react";
import { useAuth } from "react-oidc-context";
import { ApiError } from "../../api/client";
import { createCompareClient, type CompareKey } from "../../api/compareAuth";
import type { SubgraphWire } from "../../api/types";
import { useApiClient } from "../../auth/useApiClient";
import { CompareChamber } from "./CompareChamber";
import type { VerifiedInvokeRecord } from "./compareRecord";

const PLACARD =
  "Both views are exactly what the server sent each keyholder. The client never saw what was removed.";

const KEY_WITHDRAWN_EXIT_MS = 600;

export interface ComparePaneProps {
  compareKey: CompareKey;
  anchorKeys: string[];
  anchorCitations: string[];
  chunkIds: string[];
  verifiedRecord: VerifiedInvokeRecord | null;
  onExit: () => void;
}

type Phase = "querying" | "active" | "error" | "key-expired" | "withdrawn";

interface SideCounts {
  chunkCount: number | null;
  rowCount: number | null;
}

interface CompareData {
  graphA: SubgraphWire;
  graphB: SubgraphWire;
  a: SideCounts;
  b: SideCounts;
}

interface SideError {
  key: "A" | "B";
  status: number;
  detail?: string;
}

function toSideError(key: "A" | "B", err: unknown): SideError {
  if (err instanceof ApiError) return { key, status: err.status, detail: err.detail };
  return { key, status: 0 };
}

// A key-B settled result that rejected with a 401 means the compare token
// expired: onKeyExpired has ALREADY flipped the pane to the honest key-expired
// state, so the async continuation must bail rather than fall through to an
// "active" comparison with silently-missing B counts.
function isKeyBExpired(result: PromiseSettledResult<unknown>): boolean {
  return (
    result.status === "rejected" &&
    result.reason instanceof ApiError &&
    result.reason.status === 401
  );
}

export function ComparePane({
  compareKey,
  anchorKeys,
  anchorCitations,
  chunkIds,
  verifiedRecord,
  onExit,
}: ComparePaneProps) {
  const auth = useAuth();
  const clientA = useApiClient();

  const [phase, setPhase] = useState<Phase>("querying");
  const [data, setData] = useState<CompareData | null>(null);
  const [sideError, setSideError] = useState<SideError | null>(null);

  // A SEPARATE fetch context bound to the key-B bearer. Its 401 handler flips the
  // pane to an honest key-expired state — it NEVER triggers the primary
  // signinRedirect (that would clobber the operator's own session).
  const clientB = useMemo(
    () => createCompareClient(compareKey, () => setPhase("key-expired")),
    [compareKey],
  );

  // Snapshot the query inputs at mount: the compare reflects the graph as it was
  // when COMPARE was clicked, and the inputs stay stable so the paired queries
  // run exactly once (no re-fire on unrelated parent re-renders).
  const [inputs] = useState(() => ({ anchorKeys, anchorCitations, chunkIds, verifiedRecord }));

  const withdrawnRef = useRef(false);

  // Run the identical bodies under BOTH bearers. Every setState is in the async
  // continuation (after an await), per the KnowledgePage set-state-in-effect
  // precedent, so the react-hooks lint stays green.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      await Promise.resolve();
      if (cancelled) return;
      setPhase("querying");
      // ONE literal body object shared by both bearers — the identical query.
      const graphBody = {
        anchor_keys: inputs.anchorKeys,
        anchor_citations: inputs.anchorCitations,
        hops: 2,
      };
      const [ra, rb] = await Promise.allSettled([
        clientA.graphQuery(graphBody),
        clientB.graphQuery(graphBody),
      ]);
      if (cancelled) return;
      // No contraction without BOTH real graph responses (§0 law 5).
      if (ra.status === "rejected") {
        setSideError(toSideError("A", ra.reason));
        setPhase("error");
        return;
      }
      if (rb.status === "rejected") {
        // A key-B 401 already flipped the phase to key-expired via onKeyExpired.
        if (isKeyBExpired(rb)) return;
        setSideError(toSideError("B", rb.reason));
        setPhase("error");
        return;
      }
      const graphA = ra.value;
      const graphB = rb.value;

      // Side chunk counts (citations.length) under each bearer — the SAME
      // retrieve body. Null when there are no session chunks (honest line).
      let chunkA: number | null = null;
      let chunkB: number | null = null;
      if (inputs.chunkIds.length > 0) {
        const [ca, cb] = await Promise.allSettled([
          clientA.retrieve(inputs.chunkIds),
          clientB.retrieve(inputs.chunkIds),
        ]);
        if (cancelled) return;
        // A key-B 401 here already latched key-expired via onKeyExpired — keep
        // that honest state; never fall through to a false "active" pane.
        if (isKeyBExpired(cb)) return;
        if (ca.status === "fulfilled") chunkA = ca.value.citations.length;
        if (cb.status === "fulfilled") chunkB = cb.value.citations.length;
      }

      // Verified-answer row counts — re-invoke the recorded template under each
      // bearer (the SAME body). Null when nothing was invoked this session.
      let rowA: number | null = null;
      let rowB: number | null = null;
      if (inputs.verifiedRecord) {
        const invokeBody = {
          template_id: inputs.verifiedRecord.template_id,
          version: inputs.verifiedRecord.version,
          args: inputs.verifiedRecord.args,
        };
        const [va, vb] = await Promise.allSettled([
          clientA.verifiedInvoke(invokeBody),
          clientB.verifiedInvoke(invokeBody),
        ]);
        if (cancelled) return;
        // Same guard: a key-B 401 on the verified re-invoke keeps key-expired.
        if (isKeyBExpired(vb)) return;
        if (va.status === "fulfilled") rowA = va.value.answer.rows.length;
        if (vb.status === "fulfilled") rowB = vb.value.answer.rows.length;
      }

      if (cancelled) return;
      setData({
        graphA,
        graphB,
        a: { chunkCount: chunkA, rowCount: rowA },
        b: { chunkCount: chunkB, rowCount: rowB },
      });
      setPhase("active");
    })();
    return () => {
      cancelled = true;
    };
  }, [clientA, clientB, inputs]);

  // If the pane unmounts while the key is still held (navigation away), the
  // second token is discarded — it never outlives the pane.
  useEffect(() => {
    return () => {
      if (!withdrawnRef.current && compareKey.getAccessToken() !== undefined) {
        void compareKey.withdraw();
      }
    };
  }, [compareKey]);

  // KEY WITHDRAWN: discard (+ best-effort revoke) the second token, then exit.
  useEffect(() => {
    if (phase !== "withdrawn") return;
    const timer = setTimeout(onExit, KEY_WITHDRAWN_EXIT_MS);
    return () => clearTimeout(timer);
  }, [phase, onExit]);

  async function onWithdraw() {
    withdrawnRef.current = true;
    await compareKey.withdraw();
    setPhase("withdrawn");
  }

  const keyALabel = useMemo(() => {
    const profile = auth.user?.profile;
    const preferred = profile?.preferred_username;
    if (typeof preferred === "string" && preferred.length > 0) return preferred;
    return profile?.sub ?? "primary keyholder";
  }, [auth.user]);

  const bOnlyCount = useMemo(() => {
    if (!data) return 0;
    const aKeys = new Set(data.graphA.nodes.map((n) => n.key));
    return data.graphB.nodes.filter((n) => !aKeys.has(n.key)).length;
  }, [data]);

  return (
    <section className="ne-compare" aria-label="Dual-key compare">
      <header className="ne-compare__header">
        <div className="ne-compare__plates">
          <span className="ne-compare__plate">
            <span className="ne-label">KEY A</span>
            <span className="ne-mono ne-compare__persona">{keyALabel}</span>
          </span>
          <span className="ne-compare__plate ne-compare__plate--b">
            <span className="ne-label">KEY B</span>
            <span className="ne-mono ne-compare__persona">{compareKey.label}</span>
          </span>
        </div>
        <button
          type="button"
          className="ne-compare__withdraw"
          onClick={() => {
            void onWithdraw();
          }}
          disabled={phase === "withdrawn"}
        >
          WITHDRAW KEY
        </button>
      </header>

      {phase === "querying" && (
        <div className="ne-compare__body" role="status">
          <p className="ne-compare__title">Running the identical query under both keys…</p>
          <div className="ne-knowledge__sweep" aria-hidden="true" />
        </div>
      )}

      {phase === "key-expired" && (
        <div className="ne-compare__body ne-compare__body--fault" role="alert">
          <p className="ne-compare__title ne-label">KEY B EXPIRED</p>
          <p className="ne-compare__note ne-mono">
            The second key returned HTTP 401 — its token is no longer valid. Nothing was fabricated;
            withdraw the key and re-authenticate to compare again.
          </p>
        </div>
      )}

      {phase === "error" && sideError && (
        <div className="ne-compare__body ne-compare__body--fault" role="alert">
          <p className="ne-compare__title ne-label">COMPARE FAILED — FAIL-CLOSED</p>
          <p className="ne-compare__note ne-mono">
            key {sideError.key}: HTTP {sideError.status}
            {sideError.detail ? ` · ${sideError.detail}` : ""}
          </p>
          <p className="ne-compare__note">
            No trimmed view is shown — the contraction runs only over two real server responses.
          </p>
        </div>
      )}

      {phase === "active" && data && (
        <div className="ne-compare__body">
          <CompareChamber
            graphA={data.graphA}
            graphB={data.graphB}
            anchorKeys={inputs.anchorKeys}
            anchorCitations={inputs.anchorCitations}
          />

          <div className="ne-compare__stats">
            <StatRow
              label="NODES"
              a={String(data.graphA.nodes.length)}
              b={String(data.graphB.nodes.length)}
            />
            <StatRow
              label="EDGES"
              a={String(data.graphA.edges.length)}
              b={String(data.graphB.edges.length)}
            />
            {data.a.chunkCount !== null || data.b.chunkCount !== null ? (
              <StatRow
                label="CHUNKS"
                a={data.a.chunkCount === null ? "—" : String(data.a.chunkCount)}
                b={data.b.chunkCount === null ? "—" : String(data.b.chunkCount)}
              />
            ) : (
              <p className="ne-compare__unavailable">
                No chunks in session — search and anchor first
              </p>
            )}
            {data.a.rowCount !== null || data.b.rowCount !== null ? (
              <StatRow
                label="VERIFIED ROWS"
                a={data.a.rowCount === null ? "—" : String(data.a.rowCount)}
                b={data.b.rowCount === null ? "—" : String(data.b.rowCount)}
              />
            ) : (
              <p className="ne-compare__unavailable">
                No verified answer has been invoked this session — row-count compare unavailable
              </p>
            )}
          </div>

          {bOnlyCount > 0 && (
            <p className="ne-compare__bonly">{bOnlyCount} entities appear only under key B</p>
          )}

          <p className="ne-compare__placard">{PLACARD}</p>
        </div>
      )}

      {phase === "withdrawn" && (
        <button type="button" className="ne-compare__stamp" onClick={onExit} role="status">
          KEY WITHDRAWN
        </button>
      )}
    </section>
  );
}

function StatRow({ label, a, b }: { label: string; a: string; b: string }) {
  return (
    <div className="ne-compare__stat-row">
      <span className="ne-label ne-compare__stat-label">{label}</span>
      <span className="ne-mono ne-compare__stat-a">{a}</span>
      <span className="ne-mono ne-compare__stat-b">{b}</span>
    </div>
  );
}
