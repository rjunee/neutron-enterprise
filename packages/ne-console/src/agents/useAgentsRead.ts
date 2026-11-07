// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { useCallback, useEffect, useReducer, useState } from "react";

/**
 * A governed read has four outcomes, and `unavailable` is NOT a fault.
 *
 * The Agents routers answer a hidden Agent and an unknown Agent with the SAME
 * sanitized 404 (`routers/agents.py:43`, `conversations.py:31`, `runs.py:28`),
 * deliberately, so that a refusal cannot be used to prove an Agent exists. An
 * invisible Agent is simply omitted from the catalog, and a stale capability
 * hash is refused with that same 404 for the same reason.
 *
 * So this state is reached without ever learning WHICH case occurred, and the
 * console must keep it that way: one neutral presentation covers both. Nothing
 * here may consult a second endpoint, read a header, or time a response to tell
 * them apart — doing so would rebuild the existence oracle the 404 exists to
 * deny. There is deliberately no retry: a correctly governed boundary is not an
 * outage, and a Retry button that can never succeed is a dead end.
 *
 * 403 is NOT a case here. These routers never emit one.
 */
export type ReadState<T> =
  | { readonly phase: "loading" }
  | { readonly phase: "ready"; readonly value: T }
  | { readonly phase: "unavailable" }
  | { readonly phase: "error" };

function isUnavailable(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    "status" in error &&
    (error as { status?: unknown }).status === 404
  );
}

export function useAgentsRead<T>(
  load: () => Promise<T>,
  identityKey: string,
): [ReadState<T>, () => void] {
  const [revision, retry] = useReducer((value: number) => value + 1, 0);
  const [state, setState] = useReducer((_: ReadState<T>, next: ReadState<T>) => next, {
    phase: "loading",
  } as ReadState<T>);

  const reload = useCallback(() => retry(), []);
  useEffect(() => {
    let current = true;
    setState({ phase: "loading" });
    void load().then(
      (value) => current && setState({ phase: "ready", value }),
      (error: unknown) =>
        current && setState({ phase: isUnavailable(error) ? "unavailable" : "error" }),
    );
    return () => {
      current = false;
    };
  }, [load, identityKey, revision]);
  return [state, reload];
}

/**
 * Forward/back over the server's opaque cursors.
 *
 * Every `*Page` wire model carries a REQUIRED, nullable `next_cursor`, and the
 * routers default to `limit=25`. A surface that reads page one and drops the
 * cursor therefore shows the first 25 rows and silently claims to be complete —
 * which on a Run-receipt surface is a truthfulness failure, not a nicety.
 *
 * The cursor is opaque: this keeps the stack of cursors it was handed and never
 * derives, decodes or counts them, so no page number or total is invented.
 */
export function useCursorPages(): {
  readonly cursor: string | undefined;
  readonly onFirstPage: boolean;
  readonly back: () => void;
  readonly forward: (nextCursor: string | null) => void;
  readonly reset: () => void;
} {
  const [cursors, setCursors] = useState<readonly (string | null)[]>([null]);
  const back = useCallback(
    () => setCursors((value) => (value.length > 1 ? value.slice(0, -1) : value)),
    [],
  );
  const forward = useCallback((nextCursor: string | null) => {
    if (nextCursor === null) return;
    setCursors((value) => [...value, nextCursor]);
  }, []);
  const reset = useCallback(() => setCursors([null]), []);
  return {
    cursor: cursors.at(-1) ?? undefined,
    onFirstPage: cursors.length === 1,
    back,
    forward,
    reset,
  };
}
