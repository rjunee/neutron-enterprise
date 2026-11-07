// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// NE-141 — the optional verified-answer recorder context. The Dual-Key Compare
// pane re-invokes the LAST verified answer under both keys, so it needs to know
// which template + args the operator last ran. VerifiedAnswersTab records that
// here through a bounded hook. The context defaults to NULL: when no provider is
// mounted (every existing consumer/test), useCompareRecorder() returns null and
// the recorder call is a no-op, so behaviour is unchanged (additive discipline).

import { createContext, useContext } from "react";

/** A record of the last verified answer invoked this session — the minimum the
 * compare pane needs to re-invoke it under both bearers. `args` is the SAME
 * literal shape the tab already sent to /v1/verified/invoke. */
export interface VerifiedInvokeRecord {
  template_id: string;
  version: number | undefined;
  args: Record<string, unknown>;
  /** The key-A row count (for the side-by-side compare's A column). */
  row_count: number;
}

export interface CompareRecorder {
  recordVerifiedInvoke(record: VerifiedInvokeRecord): void;
}

/** Null when no compare recorder is mounted — the existing default. */
export const CompareRecordContext = createContext<CompareRecorder | null>(null);

/** Read the optional recorder. Returns null (no-op) outside a provider, so every
 * existing VerifiedAnswersTab consumer is untouched. */
export function useCompareRecorder(): CompareRecorder | null {
  return useContext(CompareRecordContext);
}
