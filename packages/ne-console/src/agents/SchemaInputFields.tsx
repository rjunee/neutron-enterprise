// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE TYPED INPUT, READ OFF THE SERVER'S OWN SCHEMA.
//
// What this replaces was a single hardcoded special case: the launcher tested
// for a property literally named `max_subjects` whose schema was exactly
// `{type: "integer", minimum: 1, maximum: 5}`, and rendered one number input
// for it. Any other workflow — any other property name, any other bound — fell
// through to "typed input is unavailable" despite the server having described
// it perfectly well.
//
// THE SUPPORTED SET IS DELIBERATELY NARROW, AND STAYS NARROW. This module
// renders bounded integers and booleans. It does NOT render a string field,
// and that omission is load-bearing rather than unfinished: an unbounded text
// input is the generic prompt box that PRE-DEMO-REHEARSAL-012 exists to keep
// off a deterministic surface, and there is no production workflow schema that
// asks for one. When a schema asks for anything outside the supported set,
// `readSchemaFields` returns null, the panel says the typed input is
// unavailable and Run is disabled. It never guesses a widget, never drops an
// unrecognized property and submits the rest, and never silently coerces.
//
// THE LABEL IS THE SERVER'S. Pydantic's `model_json_schema()` emits a `title`
// per property ("Max Subjects"), and that title is what renders. The console
// used to hardcode "Maximum subjects" for the one property it knew about, which
// is console copy asserting a fact about a server-owned form. A schema with no
// title falls back to the property name made readable, which is still derived
// from the schema rather than invented beside it.

import type { ChangeEvent } from "react";

/** One field this module knows how to render. The union is the whole
 * supported set; widening it is a deliberate act, not a default. */
export type SchemaField =
  | {
      readonly kind: "integer";
      readonly name: string;
      readonly label: string;
      readonly minimum: number;
      readonly maximum: number;
      readonly description: string | null;
    }
  | {
      readonly kind: "boolean";
      readonly name: string;
      readonly label: string;
      readonly description: string | null;
    };

export type SchemaValue = number | boolean;

/** `max_subjects` -> `Max subjects`. Only reached when the schema names no
 * title of its own. */
export function humanizeProperty(name: string): string {
  const words = name.replace(/[_-]+/g, " ").trim();
  if (words.length === 0) return name;
  return words[0]!.toUpperCase() + words.slice(1);
}

function stringOrNull(value: unknown): string | null {
  return typeof value === "string" && value.trim().length > 0 ? value : null;
}

function readField(name: string, raw: unknown): SchemaField | null {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return null;
  const schema = raw as Record<string, unknown>;
  // A composed schema (anyOf/oneOf/allOf/$ref), an enum or a const is a shape
  // this module cannot render faithfully, so it renders none of the form.
  for (const key of ["anyOf", "oneOf", "allOf", "$ref", "enum", "const"]) {
    if (key in schema) return null;
  }
  const label = stringOrNull(schema.title) ?? humanizeProperty(name);
  const description = stringOrNull(schema.description);
  if (schema.type === "boolean") {
    return { kind: "boolean", name, label, description };
  }
  if (schema.type === "integer") {
    const { minimum, maximum } = schema;
    // BOTH bounds are required. An unbounded integer has no honest widget here
    // and no production schema asks for one.
    if (typeof minimum !== "number" || typeof maximum !== "number") return null;
    if (!Number.isInteger(minimum) || !Number.isInteger(maximum)) return null;
    if (maximum < minimum) return null;
    return { kind: "integer", name, label, minimum, maximum, description };
  }
  return null;
}

/**
 * Every field of an object schema, in the schema's own property order, or null
 * when ANY part of it is outside the supported set.
 *
 * All-or-nothing is the whole safety property: rendering the three properties a
 * workflow declared and silently dropping the fourth would submit an input the
 * reader believes they filled in and the server never received.
 */
export function readSchemaFields(schema: unknown): readonly SchemaField[] | null {
  if (typeof schema !== "object" || schema === null || Array.isArray(schema)) return null;
  const root = schema as Record<string, unknown>;
  if (root.type !== "object") return null;
  const properties = root.properties;
  if (properties === undefined) return [];
  if (typeof properties !== "object" || properties === null || Array.isArray(properties)) {
    return null;
  }
  const out: SchemaField[] = [];
  for (const [name, raw] of Object.entries(properties as Record<string, unknown>)) {
    const field = readField(name, raw);
    if (field === null) return null;
    out.push(field);
  }
  // A `required` naming something that is not a rendered property means the
  // server expects a value this form cannot collect.
  const required = root.required;
  if (required !== undefined) {
    if (!Array.isArray(required)) return null;
    for (const name of required) {
      if (typeof name !== "string") return null;
      if (!out.some((field) => field.name === name)) return null;
    }
  }
  return out;
}

/** The resting value of each field: the low bound of an integer, false for a
 * boolean. Neither is invented — both are the smallest value the server's own
 * schema declares acceptable. */
export function initialSchemaValues(
  fields: readonly SchemaField[],
): Readonly<Record<string, SchemaValue>> {
  const out: Record<string, SchemaValue> = {};
  for (const field of fields) {
    out[field.name] = field.kind === "integer" ? field.minimum : false;
  }
  return out;
}

export function schemaValuesValid(
  fields: readonly SchemaField[],
  values: Readonly<Record<string, SchemaValue>>,
): boolean {
  return fields.every((field) => {
    const value = values[field.name];
    if (field.kind === "boolean") return typeof value === "boolean";
    return (
      typeof value === "number" &&
      Number.isInteger(value) &&
      value >= field.minimum &&
      value <= field.maximum
    );
  });
}

/**
 * The exact submitted body, in the schema's declared property order.
 *
 * Order matters because the request body is a string the server hashes; a
 * key-order change is a different byte sequence for the same intent.
 */
export function schemaValuesJson(
  fields: readonly SchemaField[],
  values: Readonly<Record<string, SchemaValue>>,
): string {
  const body: Record<string, SchemaValue> = {};
  for (const field of fields) {
    const value = values[field.name];
    if (value !== undefined) body[field.name] = value;
  }
  return JSON.stringify(body);
}

export function SchemaInputFields({
  fields,
  values,
  onChange,
  disabled = false,
  idPrefix,
}: {
  fields: readonly SchemaField[];
  values: Readonly<Record<string, SchemaValue>>;
  onChange: (name: string, value: SchemaValue) => void;
  disabled?: boolean;
  idPrefix: string;
}) {
  if (fields.length === 0) {
    return (
      <p className="ne-runform__noinput">
        This workflow takes no caller-supplied input. Every value is frozen in its profile.
      </p>
    );
  }
  return (
    <div className="ne-runform__fields">
      {fields.map((field) => {
        const id = `${idPrefix}-${field.name}`;
        const describedBy = field.description === null ? undefined : `${id}-desc`;
        return (
          <div key={field.name} className="ne-runform__field">
            <label htmlFor={id}>{field.label}</label>
            {field.kind === "integer" ? (
              <input
                id={id}
                type="number"
                inputMode="numeric"
                min={field.minimum}
                max={field.maximum}
                step={1}
                disabled={disabled}
                aria-describedby={describedBy}
                value={typeof values[field.name] === "number" ? String(values[field.name]) : ""}
                onChange={(event: ChangeEvent<HTMLInputElement>) =>
                  onChange(field.name, event.currentTarget.valueAsNumber)
                }
              />
            ) : (
              <input
                id={id}
                type="checkbox"
                disabled={disabled}
                aria-describedby={describedBy}
                checked={values[field.name] === true}
                onChange={(event: ChangeEvent<HTMLInputElement>) =>
                  onChange(field.name, event.currentTarget.checked)
                }
              />
            )}
            {field.kind === "integer" ? (
              <span className="ne-runform__bounds">
                {field.minimum}–{field.maximum}
              </span>
            ) : null}
            {field.description === null ? null : (
              <span className="ne-runform__desc" id={describedBy}>
                {field.description}
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}
