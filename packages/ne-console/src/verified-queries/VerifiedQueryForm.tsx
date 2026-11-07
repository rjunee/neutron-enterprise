// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { useMemo, useState } from "react";
import type { ChangeEvent, FormEvent } from "react";
import type { VerifiedTemplateWire } from "../api/types";
import { coerceArgValue, hasUnsupportedField, parseArgsSchema } from "../knowledge/t3/argsForm";
import type { ArgField } from "../knowledge/t3/argsForm";
import { PrimaryButton } from "../primitives/Toolbar";
import { snapshotInvokeRequest } from "./verifiedQueriesState";
import type { VerifiedInvokeRequestSnapshot } from "./verifiedQueriesState";

type FieldValue = string | boolean | string[];

function initialValue(field: ArgField): FieldValue {
  if (field.listMax !== undefined) return [""];
  return field.kind === "bool" && field.required ? false : "";
}

function ScalarInput({
  id,
  field,
  value,
  onChange,
}: {
  id: string;
  field: ArgField;
  value: string;
  onChange: (value: string) => void;
}) {
  const onText = (event: ChangeEvent<HTMLInputElement>) => onChange(event.target.value);
  if (field.kind === "enum") {
    return (
      <select
        id={id}
        required={field.required}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">{field.required ? "Select…" : "Use query default"}</option>
        {(field.permittedValues ?? []).map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    );
  }
  return (
    <input
      id={id}
      type={
        field.kind === "int"
          ? "number"
          : field.kind === "date"
            ? "date"
            : field.kind === "datetime"
              ? "datetime-local"
              : "text"
      }
      step={field.kind === "int" ? 1 : undefined}
      required={field.required}
      pattern={field.pattern}
      maxLength={field.maxLength}
      value={value}
      onChange={onText}
    />
  );
}

function ListFieldInput({
  field,
  values,
  onChange,
}: {
  field: ArgField;
  values: string[];
  onChange: (values: string[]) => void;
}) {
  const cap = field.listMax ?? 1;
  const label = field.title ?? field.name;
  const group = `ne-verified-query-${field.name}`;
  return (
    <fieldset className="ne-verified-queries__field ne-verified-queries__field--list">
      <legend>
        {label} {field.required ? <span aria-label="required">*</span> : <span>(optional)</span>}
      </legend>
      <p className="ne-verified-queries__list-hint">
        One value, or up to {cap} distinct values — each is validated exactly like a single value.
      </p>
      {values.map((member, index) => (
        <div className="ne-verified-queries__list-row" key={index}>
          <label className="ne-sr-only" htmlFor={`${group}-${index}`}>
            {label} value {index + 1}
          </label>
          <ScalarInput
            id={`${group}-${index}`}
            field={field}
            value={member}
            onChange={(next) => onChange(values.map((v, i) => (i === index ? next : v)))}
          />
          {values.length > 1 ? (
            <button
              type="button"
              className="ne-btn"
              aria-label={`Remove ${label} value ${index + 1}`}
              onClick={() => onChange(values.filter((_, i) => i !== index))}
            >
              Remove
            </button>
          ) : null}
        </div>
      ))}
      <button
        type="button"
        className="ne-btn"
        disabled={values.length >= cap}
        onClick={() => onChange([...values, ""])}
      >
        Add another value ({values.length} of {cap})
      </button>
    </fieldset>
  );
}

function FieldInput({
  field,
  value,
  onChange,
}: {
  field: ArgField;
  value: FieldValue;
  onChange: (value: FieldValue) => void;
}) {
  const id = `ne-verified-query-${field.name}`;
  const label = field.title ?? field.name;
  if (field.listMax !== undefined) {
    return (
      <ListFieldInput
        field={field}
        values={Array.isArray(value) ? value : [typeof value === "string" ? value : ""]}
        onChange={onChange}
      />
    );
  }
  const text = typeof value === "string" ? value : String(value);
  return (
    <div className="ne-verified-queries__field">
      <label htmlFor={id}>
        {label} {field.required ? <span aria-label="required">*</span> : <span>(optional)</span>}
      </label>
      {field.kind === "bool" && field.required ? (
        <input
          id={id}
          type="checkbox"
          checked={value === true}
          onChange={(event) => onChange(event.target.checked)}
        />
      ) : field.kind === "bool" ? (
        <select id={id} value={text} onChange={(event) => onChange(event.target.value)}>
          <option value="">Use query default</option>
          <option value="true">True</option>
          <option value="false">False</option>
        </select>
      ) : (
        <ScalarInput id={id} field={field} value={text} onChange={onChange} />
      )}
    </div>
  );
}

function coerceListMembers(
  field: ArgField,
  raw: string[],
): { ok: true; value: string | string[] } | { ok: false; reason: string } {
  const label = field.title ?? field.name;
  const members = raw.map((member) => member.trim());
  if (members.some((member) => member === "")) {
    const position = members.findIndex((member) => member === "") + 1;
    return { ok: false, reason: `${label} value ${position} is empty — fill it or remove it.` };
  }
  if (members.length === 0) return { ok: false, reason: `${label} needs at least one value.` };
  const cap = field.listMax ?? members.length;
  if (members.length > cap) {
    return { ok: false, reason: `${label} allows at most ${cap} values.` };
  }
  if (new Set(members).size !== members.length) {
    return { ok: false, reason: `${label} values must be distinct — remove the duplicate.` };
  }
  const coercedMembers: string[] = [];
  for (const [index, member] of members.entries()) {
    const coerced = coerceArgValue(field.kind, member, field.permittedValues);
    if (!coerced.ok) {
      return { ok: false, reason: `${label} value ${index + 1}: ${coerced.reason}` };
    }
    coercedMembers.push(String(coerced.value));
  }
  // A single value posts as the scalar branch (identical server semantics, and
  // byte-identical to what the pre-list form sent); two or more post as the list.
  const [single] = coercedMembers;
  return {
    ok: true,
    value: coercedMembers.length === 1 && single !== undefined ? single : coercedMembers,
  };
}

export function VerifiedQueryForm({
  template,
  running,
  onRun,
}: {
  template: VerifiedTemplateWire;
  running: boolean;
  onRun: (request: VerifiedInvokeRequestSnapshot) => void;
}) {
  const fields = useMemo(() => parseArgsSchema(template.args_schema), [template]);
  const unsupported = useMemo(() => hasUnsupportedField(fields), [fields]);
  const [values, setValues] = useState<Record<string, FieldValue>>(() =>
    Object.fromEntries(fields.map((field) => [field.name, initialValue(field)])),
  );
  const [error, setError] = useState<string | null>(null);

  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const args: Record<string, unknown> = {};
    for (const field of fields) {
      const value = values[field.name] ?? initialValue(field);
      if (Array.isArray(value)) {
        const everyBlank = value.every((member) => member.trim() === "");
        if (!field.required && everyBlank) continue;
        const coerced = coerceListMembers(field, value);
        if (!coerced.ok) {
          setError(coerced.reason);
          return;
        }
        args[field.name] = coerced.value;
        continue;
      }
      if (!field.required && value === "") continue;
      if (field.required && value === "") {
        setError(`${field.title ?? field.name} is required.`);
        return;
      }
      const coerced = coerceArgValue(field.kind, value, field.permittedValues);
      if (!coerced.ok) {
        setError(`${field.title ?? field.name}: ${coerced.reason}`);
        return;
      }
      args[field.name] = coerced.value;
    }
    setError(null);
    onRun(snapshotInvokeRequest(template, args));
  };

  if (unsupported) {
    // Honest fail-closed placard, now NAMING the offending parameter(s) and why
    // — never a guessed input, and never the old blanket sentence.
    const broken = fields.filter((field) => field.kind === "unsupported");
    return (
      <div role="note" className="ne-verified-queries__unsupported">
        <p>This form cannot safely build every input this query declares, so Run is disabled:</p>
        <ul>
          {broken.map((field) => (
            <li key={field.name}>
              <code>{field.name}</code>{" "}
              {field.unsupportedReason ?? "has a schema this form cannot render"}.
            </li>
          ))}
        </ul>
      </div>
    );
  }

  return (
    <form className="ne-verified-queries__form" onSubmit={submit}>
      <h3>Parameters</h3>
      {fields.length === 0 ? (
        <p>This query has no parameters.</p>
      ) : (
        fields.map((field) => (
          <FieldInput
            key={field.name}
            field={field}
            value={values[field.name] ?? initialValue(field)}
            onChange={(value) => setValues((current) => ({ ...current, [field.name]: value }))}
          />
        ))
      )}
      {error === null ? null : (
        <p role="alert" className="ne-state__detail">
          {error}
        </p>
      )}
      <PrimaryButton
        type="submit"
        label={running ? "Running…" : "Run verified query"}
        disabled={running}
      />
    </form>
  );
}
