// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The ONE shared /v1/me read. Mounted once in the authed shell so the status
// strip's identity chip (and anything else that needs the principal) costs a
// single request rather than one per consumer. Loading/error render honestly —
// never a fabricated identity.

import {
  createContext,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";
import type { ApiClient } from "../api/client";
import type { PrincipalView } from "../api/types";
import { useApiClient } from "./useApiClient";

export interface PrincipalState {
  status: "loading" | "ok" | "error";
  principal: PrincipalView | null;
}

const LOADING_STATE: PrincipalState = { status: "loading", principal: null };
const PrincipalContext = createContext<PrincipalState>(LOADING_STATE);

/**
 * The exact client/principal generation that owns a governed surface.
 *
 * The generation changes for either axis independently: replacing the auth
 * client changes it before `/me` settles, while a newly served principal state
 * changes it even when the client object is retained. Consumers therefore do
 * not need a second OIDC/auth read to close an ownership transition.
 */
export interface PrincipalOwner {
  readonly client: ApiClient;
  readonly principal: PrincipalState;
  readonly generation: string;
}

const PrincipalOwnerContext = createContext<PrincipalOwner | null>(null);
let nextOwnerIdentity = 0;
const ownerIdentities = new WeakMap<object, number>();

function ownerIdentity(value: object): number {
  const current = ownerIdentities.get(value);
  if (current !== undefined) return current;
  nextOwnerIdentity += 1;
  ownerIdentities.set(value, nextOwnerIdentity);
  return nextOwnerIdentity;
}

export function principalOwnerReady(owner: PrincipalOwner): boolean {
  return owner.principal.status === "ok" && owner.principal.principal !== null;
}

interface OwnedPrincipalState {
  client: ApiClient;
  state: PrincipalState;
}

export function PrincipalProvider({ children }: { children: ReactNode }) {
  const client = useApiClient();
  const currentClient = useRef(client);
  const [owned, setOwned] = useState<OwnedPrincipalState>(() => ({
    client,
    state: LOADING_STATE,
  }));

  useLayoutEffect(() => {
    currentClient.current = client;
  }, [client]);

  useEffect(() => {
    let active = true;
    void client.me().then(
      (principal) => {
        if (!active || currentClient.current !== client) return;
        setOwned({
          client: currentClient.current,
          state: { status: "ok", principal },
        });
      },
      () => {
        if (!active || currentClient.current !== client) return;
        setOwned({
          client: currentClient.current,
          state: { status: "error", principal: null },
        });
      },
    );
    return () => {
      active = false;
    };
  }, [client]);

  // Effects start after render. Never publish the prior client's settled
  // principal during that render gap: token claims and `/v1/me` must belong to
  // the exact same ApiClient/auth generation.
  const state = owned.client === client ? owned.state : LOADING_STATE;
  const generation = `${ownerIdentity(client)}:${ownerIdentity(state)}`;
  const owner = useMemo<PrincipalOwner>(
    () => ({ client, principal: state, generation }),
    [client, generation, state],
  );
  return (
    <PrincipalContext.Provider value={state}>
      <PrincipalOwnerContext.Provider value={owner}>{children}</PrincipalOwnerContext.Provider>
    </PrincipalContext.Provider>
  );
}

export function usePrincipal(): PrincipalState {
  return useContext(PrincipalContext);
}

export function usePrincipalOwner(): PrincipalOwner {
  const owner = useContext(PrincipalOwnerContext);
  if (owner === null) throw new Error("usePrincipalOwner must be used within PrincipalProvider");
  return owner;
}
