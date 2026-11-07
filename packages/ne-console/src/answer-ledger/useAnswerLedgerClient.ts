// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { createAnswerLedgerClient } from "../api/answerLedgerClient";
import type { AnswerLedgerClient } from "../api/answerLedgerClient";
import { useSessionBoundClient } from "../auth/useSessionBoundClient";

/** Retained across silent token renewals, replaced on a genuine identity
 * change — see auth/useSessionBoundClient.ts for the boundary. */
export function useAnswerLedgerClient(): AnswerLedgerClient {
  return useSessionBoundClient(createAnswerLedgerClient);
}
