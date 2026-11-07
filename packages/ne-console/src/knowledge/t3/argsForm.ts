// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The typed-args form MODEL — pure logic, unit-testable without a DOM. A
// verified-answer template carries a strict generated args JSON Schema
// (Pydantic-emitted, `additionalProperties:false`); this module derives the
// per-field kinds the VERIFIED ANSWERS tab renders, and coerces the DOM input
// values back to the JSON types the governed invoke route expects.
//
// Fail-closed by construction (§0 law 5): a field whose JSON-Schema type/format
// combination is not one we can render is flagged `unsupported` — the tab shows
// an honest "cannot generate this form" placard and disables INVOKE rather than
// guessing. iri args are NOT distinguishable from str on the wire (the server's
// _build_args_model maps BOTH to a plain string annotation — verified), so they
// render as the mono string input; the ASK prefill covers entity IRIs.

import type { ArgsFieldSchema, ArgsJsonSchema } from "../../api/types";

/** The kinds the form can render. `unsupported` is the honest fail-closed flag
 * for a schema field we cannot map to a concrete input. */
export type ArgFieldKind = "string" | "enum" | "int" | "bool" | "date" | "datetime" | "unsupported";

/** One derived form field. `pattern`/`maxLength` carry the string constraints
 * onto the DOM input (client-side hint only — the server re-validates). */
export interface ArgField {
  name: string;
  kind: ArgFieldKind;
  required: boolean;
  pattern?: string;
  maxLength?: number;
  /** Server-authored closed vocabulary. Present only for a valid string enum. */
  permittedValues?: readonly string[];
  /** The schema's own `title` — REDESIGN §7: "label = field title". A schema
   * that carries no title leaves this absent and the field falls back to its
   * name, which is the only other true thing to call it. */
  title?: string;
  /** NE-KG-MULTIKEY: the declared cardinality cap of a multi-key (list) arg —
   * present EXACTLY when the schema is the server's scalar|list union shape
   * (see `_listUnion`). The form renders a bounded repeatable input; `kind`,
   * `pattern`, `maxLength` and `permittedValues` describe EACH member (the
   * union carries identical per-element constraints on both branches). */
  listMax?: number;
  /** Honest fail-closed detail for `kind === "unsupported"`: WHY this field's
   * schema cannot be rendered — named in the placard instead of a blanket
   * sentence, so a demo operator can see which template arg is the problem. */
  unsupportedReason?: string;
}

/** Recognize the server's NE-KG-MULTIKEY union shape: `anyOf` of EXACTLY a
 * scalar branch and an array branch whose `items` carry the SAME constraints
 * (the generator reuses one constraint object, so equality is the contract).
 * Returns the scalar branch + cap, or null for any other `anyOf`. */
function listUnion(schema: ArgsFieldSchema): { scalar: ArgsFieldSchema; maxItems: number } | null {
  const anyOf = schema.anyOf;
  if (anyOf === undefined || anyOf.length !== 2) return null;
  const array = anyOf.find((branch) => branch.type === "array");
  const scalar = anyOf.find((branch) => branch !== array);
  if (array === undefined || scalar === undefined || scalar.type === "array") return null;
  const items = array.items;
  if (items === undefined || typeof array.maxItems !== "number" || array.maxItems < 1) return null;
  const sameConstraints =
    items.type === scalar.type &&
    items.pattern === scalar.pattern &&
    items.maxLength === scalar.maxLength &&
    JSON.stringify(items.enum ?? null) === JSON.stringify(scalar.enum ?? null);
  if (!sameConstraints) return null;
  return { scalar, maxItems: array.maxItems };
}

function fieldKind(schema: ArgsFieldSchema): ArgFieldKind {
  const type = schema.type;
  const format = schema.format;
  if (schema.enum !== undefined) {
    return type === "string" &&
      schema.enum.length > 0 &&
      schema.enum.every((value) => typeof value === "string" && value.length > 0)
      ? "enum"
      : "unsupported";
  }
  if (type === "integer") return "int";
  if (type === "boolean") return "bool";
  if (type === "string") {
    if (format === "date") return "date";
    if (format === "date-time") return "datetime";
    // A string with no format (or an unrecognized one) is a plain text field;
    // iri args land here (they are wire-indistinguishable from str).
    return "string";
  }
  return "unsupported";
}

function unsupportedReason(schema: ArgsFieldSchema): string {
  if (schema.anyOf !== undefined) {
    return "declares a union (anyOf) shape this form only renders for the server's scalar-or-list pattern";
  }
  if (schema.enum !== undefined) return "declares an enum this form cannot render";
  if (schema.type === undefined) return "declares no JSON-Schema type";
  return `declares JSON-Schema type '${schema.type}' this form has no input for`;
}

/** Derive the ordered list of form fields from a template's strict args schema.
 * Order follows `properties` insertion order (stable JSON object order). A
 * missing/empty `properties` yields an empty field list (a zero-arg template). */
export function parseArgsSchema(schema: ArgsJsonSchema): ArgField[] {
  const props = schema.properties ?? {};
  const required = new Set(schema.required ?? []);
  return Object.entries(props).map(([name, field]) => {
    // NE-KG-MULTIKEY: a scalar|list union renders as its ELEMENT kind plus the
    // declared cardinality cap; only str-shaped elements are legal server-side
    // (enum or pattern-bounded), so an element kind outside string/enum stays
    // honestly unsupported rather than guessed at.
    const union = listUnion(field);
    const element = union === null ? field : union.scalar;
    const kind = fieldKind(element);
    const listSupported = union === null || kind === "string" || kind === "enum";
    const effectiveKind = listSupported ? kind : "unsupported";
    return {
      name,
      kind: effectiveKind,
      required: required.has(name),
      ...(element.pattern !== undefined ? { pattern: element.pattern } : {}),
      ...(element.maxLength !== undefined ? { maxLength: element.maxLength } : {}),
      ...(element.enum !== undefined ? { permittedValues: element.enum } : {}),
      ...(field.title !== undefined ? { title: field.title } : {}),
      ...(union !== null && listSupported ? { listMax: union.maxItems } : {}),
      ...(effectiveKind === "unsupported" ? { unsupportedReason: unsupportedReason(field) } : {}),
    };
  });
}

/** True when ANY field is unsupported — the form cannot be generated safely and
 * INVOKE must be disabled (fail-closed, never guess). */
export function hasUnsupportedField(fields: ArgField[]): boolean {
  return fields.some((f) => f.kind === "unsupported");
}

export type CoerceResult =
  { ok: true; value: string | number | boolean } | { ok: false; reason: string };

/** Coerce a DOM input's raw value to the JSON type the invoke route expects.
 * `int` goes through Number() with a safe-integer gate (rejects NaN/float or
 * values JSON/JavaScript would round); `bool`
 * passes the checkbox boolean through; `date` passes the input's YYYY-MM-DD;
 * `datetime` converts a datetime-local "YYYY-MM-DDTHH:MM" to naive ISO by
 * appending ":00" (server Pydantic parses ISO); `string` passes through. */
export function coerceArgValue(
  kind: ArgFieldKind,
  raw: string | boolean,
  permittedValues?: readonly string[],
): CoerceResult {
  switch (kind) {
    case "int": {
      const text = typeof raw === "boolean" ? String(raw) : raw.trim();
      if (text === "") return { ok: false, reason: "required integer is empty" };
      const n = Number(text);
      if (!Number.isSafeInteger(n)) return { ok: false, reason: `'${text}' is not an integer` };
      return { ok: true, value: n };
    }
    case "bool":
      return { ok: true, value: typeof raw === "boolean" ? raw : raw === "true" };
    case "date": {
      const text = typeof raw === "boolean" ? String(raw) : raw;
      if (text === "") return { ok: false, reason: "required date is empty" };
      return { ok: true, value: text };
    }
    case "datetime": {
      const text = typeof raw === "boolean" ? String(raw) : raw;
      if (text === "") return { ok: false, reason: "required datetime is empty" };
      // datetime-local emits "YYYY-MM-DDTHH:MM" (no seconds); append ":00" so
      // the server's Pydantic gets a full naive ISO timestamp.
      const iso = /T\d{2}:\d{2}$/.test(text) ? `${text}:00` : text;
      return { ok: true, value: iso };
    }
    case "string":
      return { ok: true, value: typeof raw === "boolean" ? String(raw) : raw };
    case "enum": {
      const text = typeof raw === "boolean" ? String(raw) : raw;
      if (permittedValues?.includes(text) === true) return { ok: true, value: text };
      return {
        ok: false,
        reason: `must be one of: ${(permittedValues ?? []).join(", ")}`,
      };
    }
    case "unsupported":
      return { ok: false, reason: "unsupported arg type" };
  }
}
