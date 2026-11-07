// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The ONE governed read behind §7's entity record, in its own module so the
// components that render the record export nothing but components.
//
// The record is the entity's 1-hop neighborhood and nothing else: its own node
// is the header and the properties, the edges around it are the relations. A
// second read would be a second answer, and two answers to "what is this
// entity" is how the header and the table start disagreeing.

import { useCallback, useEffect, useState } from "react";
import type { GraphNodeWire } from "../api/types";
import { useApiClient } from "../auth/useApiClient";
import { findEntity, relationRows } from "./record";
import type { RelationRow } from "./record";
import { classifyGraphFailure } from "./readState";
import type { ReadState } from "./readState";

export interface EntityRecordData {
  /** The anchored entity's own node, or null when the window holds none. */
  node: GraphNodeWire | null;
  relations: RelationRow[];
  /** The server says the one-hop neighborhood was capped. */
  truncated: boolean;
}

/**
 * §7: "Relations table (from `graphNeighborhood(entity, {hops:1})`)". One read,
 * and the header, the properties and the relations all come out of it.
 */
export function useEntityRecord(entityKey: string | null): {
  state: ReadState<EntityRecordData>;
  retry: () => void;
} {
  const client = useApiClient();
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<ReadState<EntityRecordData>>(
    entityKey === null
      ? { phase: "ok", value: { node: null, relations: [], truncated: false } }
      : { phase: "loading" },
  );

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      // The deferred continuation: every setState lands after an await, which is
      // the pattern the rest of this console's governed reads use.
      await Promise.resolve();
      if (cancelled) return;
      if (entityKey === null) {
        setState({ phase: "ok", value: { node: null, relations: [], truncated: false } });
        return;
      }
      setState({ phase: "loading" });
      try {
        const sub = await client.graphNeighborhood(entityKey, { hops: 1 });
        if (cancelled) return;
        setState({
          phase: "ok",
          value: {
            node: findEntity(sub, entityKey),
            relations: relationRows(sub, entityKey),
            truncated: sub.truncated,
          },
        });
      } catch (error) {
        if (cancelled) return;
        setState({ phase: classifyGraphFailure(error) });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client, entityKey, attempt]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);
  return { state, retry };
}
