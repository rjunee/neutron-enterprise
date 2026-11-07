// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { useCallback, useLayoutEffect, useMemo, useRef } from "react";
import type { ApiClient } from "../api/client";
import { usePrincipal } from "../auth/PrincipalContext";
import { useApiClient } from "../auth/useApiClient";

/** The exact authenticated read/write owner of an approval surface.
 *
 * `generation` advances synchronously during render whenever the ApiClient or
 * the server-served principal status/id changes. State carrying an older owner
 * can therefore be hidden in that same render, before passive cleanup runs.
 */
export interface ApprovalOwner {
  client: ApiClient;
  principalStatus: "loading" | "ok" | "error";
  principalId: string | null;
  generation: string;
}

let nextIdentity = 0;
const identities = new WeakMap<object, number>();

function identityOf(value: object): number {
  const current = identities.get(value);
  if (current !== undefined) return current;
  nextIdentity += 1;
  identities.set(value, nextIdentity);
  return nextIdentity;
}

function sameIdentity(a: ApprovalOwner, b: ApprovalOwner): boolean {
  return (
    a.client === b.client &&
    a.principalStatus === b.principalStatus &&
    a.principalId === b.principalId
  );
}

/** Exact equality for state produced by one `useApprovalOwner` instance. */
export function sameApprovalOwner(a: ApprovalOwner, b: ApprovalOwner): boolean {
  return sameIdentity(a, b) && a.generation === b.generation;
}

export function approvalOwnerReady(owner: ApprovalOwner): boolean {
  return owner.principalStatus === "ok" && owner.principalId !== null;
}

export function useApprovalOwner(): ApprovalOwner {
  const client = useApiClient();
  const principal = usePrincipal();
  const principalId = principal.status === "ok" ? (principal.principal?.id ?? null) : null;
  const generation = `${identityOf(client)}:${identityOf(principal)}`;
  return useMemo(
    () => ({
      client,
      principalStatus: principal.status,
      principalId,
      generation,
    }),
    [client, generation, principal.status, principalId],
  );
}

export interface ApprovalRequestTicket {
  owner: ApprovalOwner;
  requestId: string;
  requestGeneration: number;
}

interface RequestSlot {
  owner: ApprovalOwner;
  requestId: string;
  requestGeneration: number;
}

/** Latest-request gate bound synchronously to one owner and one opaque request.
 * Retained callbacks must present the owner/request they closed over; once the
 * render owner moves, `begin` refuses them before they can issue a governed
 * read or write. A later retry supersedes both stale resolves and stale rejects.
 */
export function useApprovalRequestGate(owner: ApprovalOwner, requestId: string) {
  const slotRef = useRef<RequestSlot>({ owner, requestId, requestGeneration: 0 });
  // Layout ownership closes before the browser can dispatch an event and well
  // before passive cleanup. Rendered bytes are gated separately by exact owner
  // comparison, so neither path depends on a passive effect.
  useLayoutEffect(() => {
    const current = slotRef.current;
    if (!sameApprovalOwner(current.owner, owner) || current.requestId !== requestId) {
      slotRef.current = {
        owner,
        requestId,
        requestGeneration: current.requestGeneration + 1,
      };
    }
    return () => {
      slotRef.current = {
        ...slotRef.current,
        requestGeneration: slotRef.current.requestGeneration + 1,
      };
    };
  }, [owner, requestId]);

  const begin = useCallback((expectedOwner: ApprovalOwner, expectedRequestId: string) => {
    const slot = slotRef.current;
    if (
      !sameApprovalOwner(slot.owner, expectedOwner) ||
      slot.requestId !== expectedRequestId ||
      !approvalOwnerReady(expectedOwner)
    ) {
      return null;
    }
    const next: RequestSlot = { ...slot, requestGeneration: slot.requestGeneration + 1 };
    slotRef.current = next;
    return {
      owner: next.owner,
      requestId: next.requestId,
      requestGeneration: next.requestGeneration,
    } satisfies ApprovalRequestTicket;
  }, []);

  const owns = useCallback((ticket: ApprovalRequestTicket) => {
    const slot = slotRef.current;
    return (
      sameApprovalOwner(slot.owner, ticket.owner) &&
      slot.requestId === ticket.requestId &&
      slot.requestGeneration === ticket.requestGeneration
    );
  }, []);

  const invalidate = useCallback(
    (ticket: ApprovalRequestTicket) => {
      if (owns(ticket)) {
        slotRef.current = {
          ...slotRef.current,
          requestGeneration: slotRef.current.requestGeneration + 1,
        };
      }
    },
    [owns],
  );

  return { begin, owns, invalidate };
}
