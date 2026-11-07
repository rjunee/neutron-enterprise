// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// Human names are secondary governed reads, batched and cached here. A label
// registers an opaque approval/audit RECORD reference; the client never sends
// the principal id it wants named. The server re-derives that id from a record
// the reader's role already permits, closing the raw-id oracle reverted in pull request 401.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";
import type { PrincipalReferenceScope } from "../api/types";
import type { ApprovalOwner } from "../approvals/approvalOwner";
import {
  approvalOwnerReady,
  sameApprovalOwner,
  useApprovalOwner,
} from "../approvals/approvalOwner";

export interface PrincipalReference {
  scope: PrincipalReferenceScope;
  reference: string;
}

export interface DirectoryEntry {
  name: string;
  removed: boolean;
}

interface DirectoryValue {
  lookup(id: string): DirectoryEntry | undefined;
  request(id: string, reference: PrincipalReference): void;
  version: number;
}

const NOOP: DirectoryValue = { lookup: () => undefined, request: () => {}, version: 0 };
const DirectoryContext = createContext<DirectoryValue>(NOOP);

export const RESOLVED_TTL_MS = 5 * 60_000;
export const NEGATIVE_TTL_MS = 30_000;
const MAX_BATCH = 500;

interface CacheSlot {
  entry: DirectoryEntry | null;
  expiresAt: number;
}

interface PendingReference extends PrincipalReference {
  id: string;
  owner: ApprovalOwner;
}

function requestKey(value: PendingReference): string {
  return `${value.owner.generation}:${value.scope}:${value.reference}`;
}

function cacheKey(owner: ApprovalOwner, principalId: string): string {
  return `${owner.generation}:${principalId}`;
}

export function DirectoryProvider({ children }: { children: ReactNode }) {
  const owner = useApprovalOwner();
  const currentOwnerRef = useRef(owner);
  useLayoutEffect(() => {
    currentOwnerRef.current = owner;
  }, [owner]);
  const cacheRef = useRef<Map<string, CacheSlot>>(new Map());
  const pendingRef = useRef<Map<string, PendingReference>>(new Map());
  const inFlightRef = useRef<Set<string>>(new Set());
  const timerRef = useRef<Map<string, number>>(new Map());
  const mountedRef = useRef(true);
  const [boundVersion, setBoundVersion] = useState({ owner, value: 0 });
  const version = sameApprovalOwner(boundVersion.owner, owner) ? boundVersion.value : 0;

  const cacheNegative = useCallback((expectedOwner: ApprovalOwner, id: string, now: number) => {
    const key = cacheKey(expectedOwner, id);
    const existing = cacheRef.current.get(key);
    if (existing !== undefined && existing.entry !== null && existing.expiresAt > now) return;
    cacheRef.current.set(key, { entry: null, expiresAt: now + NEGATIVE_TTL_MS });
  }, []);

  useEffect(
    () => () => {
      mountedRef.current = false;
      for (const timer of timerRef.current.values()) window.clearTimeout(timer);
      timerRef.current.clear();
    },
    [],
  );

  const flush = useCallback(async () => {
    const requests = [...pendingRef.current.entries()].flatMap(([key, item]) => {
      if (!sameApprovalOwner(item.owner, owner)) return [];
      pendingRef.current.delete(key);
      return [item];
    });
    if (requests.length === 0) return;
    if (!sameApprovalOwner(currentOwnerRef.current, owner)) return;
    for (const item of requests) inFlightRef.current.add(requestKey(item));
    const now = Date.now();
    try {
      for (const scope of ["approvals", "audit"] as const) {
        const scoped = requests.filter((item) => item.scope === scope);
        for (let at = 0; at < scoped.length; at += MAX_BATCH) {
          const batch = scoped.slice(at, at + MAX_BATCH);
          const page = await owner.client.resolvePrincipals(
            scope,
            batch.map((item) => item.reference),
          );
          if (!mountedRef.current || !sameApprovalOwner(currentOwnerRef.current, owner)) return;
          const returned = new Set<string>();
          for (const row of page.resolved) {
            returned.add(row.id);
            for (const item of batch) {
              if (item.id === row.id) {
                cacheRef.current.set(cacheKey(owner, row.id), {
                  entry: { name: row.display_name, removed: row.removed },
                  expiresAt: now + RESOLVED_TTL_MS,
                });
              }
            }
          }
          for (const item of batch) {
            if (!returned.has(item.id)) {
              cacheNegative(owner, item.id, now);
            }
          }
        }
      }
    } catch {
      if (mountedRef.current && sameApprovalOwner(currentOwnerRef.current, owner)) {
        for (const item of requests) cacheNegative(owner, item.id, now);
      }
    } finally {
      for (const item of requests) inFlightRef.current.delete(requestKey(item));
      if (mountedRef.current && sameApprovalOwner(currentOwnerRef.current, owner)) {
        setBoundVersion((previous) => ({
          owner,
          value: sameApprovalOwner(previous.owner, owner) ? previous.value + 1 : 1,
        }));
      }
    }
  }, [cacheNegative, owner]);

  const request = useCallback(
    (id: string, reference: PrincipalReference) => {
      if (
        !approvalOwnerReady(owner) ||
        !sameApprovalOwner(currentOwnerRef.current, owner) ||
        !id ||
        !reference.reference
      ) {
        return;
      }
      const cached = cacheRef.current.get(cacheKey(owner, id));
      if (cached && cached.expiresAt > Date.now()) return;
      const pending = { ...reference, id, owner };
      const key = requestKey(pending);
      if (pendingRef.current.has(key) || inFlightRef.current.has(key)) return;
      pendingRef.current.set(key, pending);
      if (!timerRef.current.has(owner.generation)) {
        const timer = window.setTimeout(() => {
          timerRef.current.delete(owner.generation);
          void flush();
        }, 0);
        timerRef.current.set(owner.generation, timer);
      }
    },
    [flush, owner],
  );

  const lookup = useCallback(
    (id: string): DirectoryEntry | undefined => {
      if (!approvalOwnerReady(owner)) return undefined;
      const slot = cacheRef.current.get(cacheKey(owner, id));
      if (slot && slot.entry !== null && slot.expiresAt > Date.now()) return slot.entry;
      return undefined;
    },
    [owner],
  );

  const value = useMemo(() => ({ lookup, request, version }), [lookup, request, version]);
  return <DirectoryContext.Provider value={value}>{children}</DirectoryContext.Provider>;
}

export function useDirectory(): DirectoryValue {
  return useContext(DirectoryContext);
}

export function usePrincipalName(
  id: string,
  reference: PrincipalReference | undefined,
): DirectoryEntry | undefined {
  const directory = useContext(DirectoryContext);
  const scope = reference?.scope;
  const recordReference = reference?.reference;
  useEffect(() => {
    if (scope !== undefined && recordReference !== undefined) {
      directory.request(id, { scope, reference: recordReference });
    }
  }, [directory, id, recordReference, scope]);
  return directory.lookup(id);
}
