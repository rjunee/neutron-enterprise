// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// Pure display formatter for a served metric value + its server-declared unit.
// The unit is authoritative: a ratio renders to 3dp, an hours floor to 1dp, a
// count as a grouped integer (§2.7 — thousands group from 10,000 up). There is
// NO client-side derivation of the value itself, only its presentation.

import { formatCount, formatMoney } from "../primitives/format";

/** Format a metric numeral for display, keyed off the server's `unit`. */
export function formatKpiValue(value: number, unit: string | null): string {
  switch (unit) {
    case "ratio":
      return value.toFixed(3);
    // C8 spend. A 0 price renders "Unpriced" (the §2.7 / SPEC §11 rule): a
    // self-hosted lane is not in the provider cost map, so its calls arrive
    // priced at zero, and drawing that as $0.00 tells a buyer the month was free
    // — a different and false claim from "we hold no price for these calls".
    //
    // A NON-ZERO price below a cent gets EXTRA PRECISION instead of the shared
    // 2dp money shape. `formatMoney` rounds a real 0.004 charge to "$0.00" — the
    // very string this screen exists to avoid, reached from the other direction
    // and indistinguishable from the unpriced case. The server keeps 8dp because
    // frontier per-call costs run to ~1e-7 USD; rounding that to free at the last
    // step would throw the precision away exactly where a buyer reads it.
    case "usd": {
      if (value !== 0 && Number.isFinite(value) && Math.abs(value) < 0.01) {
        return `${value < 0 ? "-" : ""}$${Math.abs(value).toFixed(6)}`;
      }
      return formatMoney(value);
    }
    case "hours":
      return value.toFixed(1);
    case "count":
      return formatCount(Math.round(value));
    default:
      return Number.isInteger(value) ? formatCount(value) : value.toFixed(2);
  }
}
