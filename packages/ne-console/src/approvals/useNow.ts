// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// A ticking clock hook: returns Date.now() and re-renders every intervalMs so
// SLA/expiry countdowns stay live. This is a RENDER-ONLY convenience — the
// authoritative SLA, escalation, and expiry semantics live in the NE-035
// DurableApprovalGate server-side; the console never decides an approval's fate
// from this clock (an at-zero row disables its buttons as a courtesy, but the
// server still governs the actual decision).

import { useEffect, useState } from "react";

export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}
