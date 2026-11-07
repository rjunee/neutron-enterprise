// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §8 STEP 2 — THE GOVERNED PAYLOAD READ, ON DEMAND AND PER REQUEST.
//
// NOTHING IS FETCHED UNTIL THE OPERATOR ASKS. The disclosure read is audited on
// the server: a read on pane-open would write an audit record every time
// somebody glanced at a row, and the record of who read a wire transfer's
// counterparty would stop meaning anything. `reveal()` is called from a click
// and from nowhere else — this hook starts no effect and no poll.
//
// THE STATE IS BOUND TO THE ID IT WAS READ FOR. The pane is one component that
// re-renders for whichever row is open, so a disclosure held as loose state
// would render row A's counterparty under row B's action. The state is stored
// WITH its request id and read back only when the ids match; anything else is
// IDLE. That check is the whole safety property, and it also disarms the slow
// response: a read for A that lands after the pane moved to B is stored under
// A's id, where B's render cannot see it.
//
// NO CACHE ACROSS REQUESTS (C7). One id, one disclosure, no map, no store that
// outlives the pane.
//
// 015A0-ENFORCEMENT: THE SERVER'S DISCLOSURE STATUS IS THE STATE. A read that
// answers `content_withheld` or `retryable_unknown` overwrites this hook's one
// entry, so the pane cannot keep rendering detail the gate has just withdrawn.
// Nothing here decides disclosure; `disclosureFromPayload` reads the
// discriminant the server wrote.

import { useCallback, useState } from "react";
import { IDLE, disclosureFromError, disclosureFromPayload } from "./detail";
import type { Disclosure } from "./detail";
import type { ApprovalOwner } from "./approvalOwner";
import { sameApprovalOwner, useApprovalOwner, useApprovalRequestGate } from "./approvalOwner";

interface Entry {
  owner: ApprovalOwner;
  requestId: string;
  state: Disclosure;
}

export interface DisclosureControls {
  /** The disclosure for `requestId` — IDLE for any request not yet revealed. */
  disclosure: Disclosure;
  /** Runs the governed, audited read. Called from an operator action only. */
  reveal: () => void;
}

export function useDisclosure(requestId: string): DisclosureControls {
  const owner = useApprovalOwner();
  const gate = useApprovalRequestGate(owner, requestId);
  const [entry, setEntry] = useState<Entry>({ owner, requestId, state: IDLE });

  const reveal = useCallback(() => {
    const ticket = gate.begin(owner, requestId);
    if (ticket === null) return;
    setEntry({ owner, requestId, state: { kind: "loading" } });
    void owner.client.readApprovalPayload(requestId).then(
      (body) => {
        if (gate.owns(ticket)) {
          // The gate's own disclosure status decides what this becomes. A
          // withheld answer REPLACES whatever was on screen: the entry is
          // overwritten, so a previously revealed payload for this same
          // request cannot survive the read that withdrew it.
          setEntry({ owner, requestId, state: disclosureFromPayload(body) });
        }
      },
      (err: unknown) => {
        if (gate.owns(ticket)) {
          setEntry({ owner, requestId, state: disclosureFromError(err) });
        }
      },
    );
  }, [gate, owner, requestId]);

  return {
    disclosure:
      sameApprovalOwner(entry.owner, owner) && entry.requestId === requestId ? entry.state : IDLE,
    reveal,
  };
}
