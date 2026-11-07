// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import {
  createConversationLedgerClient,
  type ConversationLedgerClient,
} from "../api/conversationLedgerClient";
import { useSessionBoundClient } from "../auth/useSessionBoundClient";

/** Retained across silent token renewals, replaced on a genuine identity change.
 *
 * The subject is still part of the client's identity — a provider that rotates
 * identities without rotating the access-token object must mint a new client and
 * read owner — but the ACCESS TOKEN no longer is: keying on it rebuilt this
 * client on every renewal. auth/authIdentity.ts holds that distinction. */
export function useConversationLedgerClient(): ConversationLedgerClient {
  return useSessionBoundClient(createConversationLedgerClient);
}
