// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE ACTION, IN WORDS. The classification itself is exercised through
// ledger.test.ts; this file is the LABEL — the thing a buyer actually reads on
// Home and on /audit, where the raw `api.audit.unauthorized` used to be.
//
// The load-bearing case is the LAST describe: a code this map has never seen
// must still read as a sentence. The vocabulary is generated per deployment
// (`read.kg.*`) and grows with every API surface, so "unknown" is the normal
// case, not the exceptional one.

import { describe, expect, it } from "vitest";
import { eventActionCode, eventActionLabel } from "./eventClass";

const ev = (action: string | null, event_type = "audit.record") => ({ action, event_type });

describe("eventActionCode", () => {
  it("is the action, and the event type only when there is no action", () => {
    expect(eventActionCode(ev("tool.execute"))).toBe("tool.execute");
    expect(eventActionCode(ev(null, "pdp.decide"))).toBe("pdp.decide");
  });

  it("treats a BLANK action as no action at all", () => {
    // `AuditRecordView.action` is `str | None` with no `min_length`
    // (`wire.py:1041`), so `""` is on the wire and `??` waves it through. An
    // empty string is not an identifier: it is an empty Action cell, an empty
    // hover title and an empty sort key, all of which read as a broken console
    // rather than as a record that named nothing.
    expect(eventActionCode(ev(""))).toBe("audit.record");
    expect(eventActionCode(ev("   "))).toBe("audit.record");
    expect(eventActionCode(ev("\t\n"))).toBe("audit.record");
  });
});

describe("eventActionLabel — the enumerated core", () => {
  it("speaks the decision vocabulary", () => {
    expect(eventActionLabel(ev("pdp.decide"))).toBe("Policy decision");
    expect(eventActionLabel(ev("approval.granted"))).toBe("Approval granted");
    expect(eventActionLabel(ev("approval.rejected"))).toBe("Approval rejected");
    expect(eventActionLabel(ev("ne.gateway.complete"))).toBe("Model call completed");
  });

  it("names the reason on each of the two tool denials", () => {
    // `denied` in the Outcome column does not say WHICH gate said no, and these
    // are different gates: an unregistered tool and an out-of-set tool.
    expect(eventActionLabel(ev("tool.unknown"))).toBe("Refused: tool is not registered");
    expect(eventActionLabel(ev("tool.toolset_denied"))).toBe(
      "Refused: tool is outside this agent's toolset",
    );
    expect(eventActionLabel(ev("tool.execute"))).toBe("Tool run");
  });

  it("reads the older stored approval spelling as the same verdict", () => {
    expect(eventActionLabel(ev("approval.approved"))).toBe("Approval granted");
  });

  it("prints no dotted code for anything in the core", () => {
    for (const code of ["pdp.decide", "tool.execute", "approval.expired", "ne.gateway.stream"]) {
      expect(eventActionLabel(ev(code))).not.toMatch(/[a-z]\.[a-z]/);
    }
  });
});

describe("eventActionLabel — the open `*.unauthorized` family", () => {
  it("names the refusal without inventing a verb, and keeps the surface", () => {
    expect(eventActionLabel(ev("api.audit.unauthorized"))).toBe("Audit access refused");
    expect(eventActionLabel(ev("api.models.routing.unauthorized"))).toBe(
      "Models routing access refused",
    );
    // A surface nobody has added yet, on the same rule.
    expect(eventActionLabel(ev("api.zephyr.unauthorized"))).toBe("Zephyr access refused");
  });

  it("still says the fact when there is no surface segment to name", () => {
    expect(eventActionLabel(ev("unauthorized"))).toBe("Access refused");
  });

  it("reads the event type when the record carries no action", () => {
    expect(eventActionLabel(ev(null, "api.audit.unauthorized"))).toBe("Audit access refused");
  });

  // 🔴 THE REGRESSION GUARD. This cell used to render every member of the family
  // as "<surface> read refused", and `authz.require_role` is mounted on WRITES:
  // `POST /v1/approvals/{id}/approve` emits `api.approval.unauthorized`
  // (`routers/approvals.py:690,830`), `POST /v1/network/air-gap` emits
  // `api.network.airgap.unauthorized` (`network.py:115,125`), and `POST
  // /v1/models/lifecycle/{id}/transition` emits `api.models.lifecycle.unauthorized`
  // (`models.py:459,471`). Each decorator was read to confirm the verb. So a
  // refused attempt to DECIDE a tier-3 approval was published on the compliance
  // ledger as a refused LOOK — a claim the record does not make. The audit record
  // carries no verb for these, so neither may this cell.
  it("never claims a read for a refusal the server emitted from a WRITE route", () => {
    const writeRefusals = [
      "api.approval.unauthorized",
      "api.network.airgap.unauthorized",
      "api.models.lifecycle.unauthorized",
      "api.connect.sync.unauthorized",
    ];
    for (const code of writeRefusals) {
      const label = eventActionLabel(ev(code));
      expect(label).not.toMatch(/\bread\b/i);
      expect(label).toMatch(/refused$/);
    }
    expect(eventActionLabel(ev("api.approval.unauthorized"))).toBe("Approval access refused");
  });

  // The same wording must hold for a refusal that really IS a GET, so the fix is
  // one honest phrase for the family rather than a per-surface lookup that would
  // go stale the day a route changes verb.
  it("uses the same wording for a refusal that is genuinely a read", () => {
    expect(eventActionLabel(ev("api.connect.quarantine.unauthorized"))).toBe(
      "Connect quarantine access refused",
    );
  });
});

describe("eventActionLabel — the unknown-code fallback", () => {
  // THE POINT OF THE DESIGN. A code this file has never seen renders as its own
  // words, never as "Unknown event" and never as an em dash: that would delete
  // the only account of what happened the console holds, on the one screen
  // whose whole job is to account for what happened.
  it("re-spaces an unrecognised code into a sentence", () => {
    expect(eventActionLabel(ev("connector.acl_quarantine"))).toBe("Connector acl quarantine");
    expect(eventActionLabel(ev("read.kg.payments_due_window"))).toBe("Read kg payments due window");
    expect(eventActionLabel(ev("memory.promotion.promoted"))).toBe("Memory promotion promoted");
  });

  it("holds for a code invented after this map was written", () => {
    expect(eventActionLabel(ev("zephyr.widget.recalibrated"))).toBe("Zephyr widget recalibrated");
  });

  it("never renders empty, a placeholder, or a leftover separator", () => {
    // WELL-FORMED codes only. This loop used to be the whole floor and it could
    // not fail: every input was already a dotted lowercase phrase, so it proved
    // the easy half of the contract and nothing about the codes that actually
    // broke it (`""`, `"api."`, `"."`). That is the assertion-that-cannot-fail
    // pattern; the degenerate table in the next describe is the half that bites.
    for (const code of [
      "connector.acl_quarantine",
      "zephyr.widget.recalibrated",
      "heartbeat",
      "read.kg.overdue_provisional_credit_disputes",
    ]) {
      const label = eventActionLabel(ev(code));
      expect(label).not.toBe("");
      expect(label).not.toMatch(/unknown|—|_|\./i);
      expect(label[0]).toBe(label[0]!.toUpperCase());
    }
  });

  it("degrades to the action itself, capitalised, for a single-segment code", () => {
    expect(eventActionLabel(ev("scram"))).toBe("Scram");
  });

  it("drops the `api.`/`ne.` namespace, which is not a word anybody reads", () => {
    expect(eventActionLabel(ev("api.chat.turn_replayed"))).toBe("Chat turn replayed");
  });
});

describe("eventActionLabel — degenerate codes, where the tier-3 floor is real", () => {
  // The floor is a claim about ANY string, because `action` is `str | None`
  // with no `min_length` and `event_type` is a bare `str`: whatever an emitter
  // (or a connector somebody wrote this morning) puts there arrives here
  // unvalidated. These are the inputs that broke it.
  const DEGENERATE: Array<{ name: string; code: string }> = [
    { name: 'empty — `action: ""`, which `??` does not catch', code: "" },
    { name: "whitespace only", code: "   " },
    { name: "whitespace only, non-space", code: "\t\n" },
    { name: "one separator", code: "." },
    { name: "the other separator", code: "_" },
    { name: "separators only", code: "..." },
    { name: "separators only, mixed", code: "._." },
    { name: "namespace only — the NAMESPACE strip is what empties it", code: "api." },
    { name: "the other namespace, only", code: "ne." },
    { name: "an Object.prototype accessor: a bare index returns an OBJECT", code: "__proto__" },
    { name: "an Object.prototype member: a bare index returns a FUNCTION", code: "constructor" },
    { name: "a prototype key that `toLowerCase` happens to save", code: "toString" },
    { name: "another one", code: "valueOf" },
    { name: "and another", code: "hasOwnProperty" },
    { name: "very long", code: "a".repeat(512) },
    { name: "non-ASCII with no upper case", code: "接続.同期" },
    { name: "non-ASCII with one", code: "рефус.чтение" },
    { name: "regex metacharacters — the code is a subject, never a pattern", code: "(a|b)*+?[" },
    { name: "format specifiers", code: "%s.%d" },
    { name: "a lone backslash", code: "\\" },
  ];

  it.each(DEGENERATE)("holds for a code that is $name", ({ code }) => {
    const label = eventActionLabel(ev(code));
    // A STRING. `EXACT_LABELS[code.toLowerCase()]` is typed `: string` and is
    // not one for `constructor`/`__proto__`; React logs an error for the
    // function and THROWS "Objects are not valid as a React child" for the
    // object, which takes the ledger page down (`AuditPage.tsx:763`).
    expect(typeof label).toBe("string");
    // Never blank: an empty Action cell is the console deleting the only
    // account of the record that it has.
    expect(label.trim()).not.toBe("");
    // Never a bare token or a leftover separator: `api.` and `.` are raw codes
    // with the punctuation still attached, which reads as a rendering bug.
    expect(label).not.toMatch(/^[\s._]+$/);
    expect(label).not.toMatch(/[._]$/);
  });

  it("reads an empty action as the record's own type, not an empty cell", () => {
    expect(eventActionLabel(ev(""))).toBe("Audit record");
    expect(eventActionLabel(ev("   ", "api.audit.unauthorized"))).toBe("Audit access refused");
  });

  it("keeps a namespace-only code's one word instead of printing `api.`", () => {
    // The namespace strip exists to remove a word nobody reads. It must never
    // be the thing that empties the phrase: here the namespace is all there is.
    expect(eventActionLabel(ev("api."))).toBe("Api");
    expect(eventActionLabel(ev("ne."))).toBe("Ne");
  });

  it("says a separator-only code has no name rather than printing the separator", () => {
    for (const code of [".", "_", "...", "._."]) {
      expect(eventActionLabel(ev(code))).toBe("Unnamed action");
    }
  });

  it("returns a string for a prototype key, not a function or Object.prototype", () => {
    expect(eventActionLabel(ev("constructor"))).toBe("Constructor");
    expect(eventActionLabel(ev("__proto__"))).toBe("Proto");
    expect(eventActionLabel(ev("toString"))).toBe("ToString");
    // The one that would have taken the page down, stated as the render does it.
    expect(typeof eventActionLabel(ev("__proto__"))).toBe("string");
  });

  it("does not let a degenerate surface turn `.unauthorized` into the floor", () => {
    // Tier 2 needs "no surface" and "a surface with no words" to stay the same
    // answer, so the floor phrase must not leak into a refusal sentence.
    expect(eventActionLabel(ev(".unauthorized"))).toBe("Access refused");
    expect(eventActionLabel(ev("..unauthorized"))).toBe("Access refused");
    expect(eventActionLabel(ev("_.unauthorized"))).toBe("Access refused");
  });

  it("speaks a long or non-ASCII code as its own words, unabridged", () => {
    // Not truncated: tier 3 exists so the console never deletes the emitter's
    // words, and the cell's own layout is where length is a layout problem.
    expect(eventActionLabel(ev("接続.同期"))).toBe("接続 同期");
    expect(eventActionLabel(ev("рефус.чтение"))).toBe("Рефус чтение");
    expect(eventActionLabel(ev(`connector.${"x".repeat(400)}`))).toBe(
      `Connector ${"x".repeat(400)}`,
    );
  });

  it("survives a padded code rather than dropping it to the floor", () => {
    expect(eventActionLabel(ev("  pdp.decide  "))).toBe("Policy decision");
  });
});
