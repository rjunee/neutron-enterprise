// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE DETERMINISTIC EXECUTION SURFACE: THE COMPOSER *IS* THE RUN FORM.
//
// The owner's complaint, in his words: "if an agent is 'deterministic' that
// should be shown prominently in the UI, and its execution may look different
// than a generic text box (e.g. fill out some fields, press run or whatever)."
//
// What shipped instead was a generic prompt box for agentic work and, for
// deterministic work, two `<pre>{JSON.stringify(...)}</pre>` dumps of the input
// schema and the frozen inputs sitting above a button labelled "Continue
// Conversation". Everything the reader needed was on screen and none of it was
// legible, which is the specific failure PRE-DEMO-REHEARSAL-012 names: "A
// generic prompt box with a decorative mode badge is not completion."
//
// So this is not a panel that sits BESIDE a composer. Where the agentic agent
// has a prompt box, this agent has this form, and there is nothing else in the
// composer slot. That is the decision, and the round-4 render is built on it.
//
// THIS FORM CONTAINS NO `<textarea>`, AND THAT IS AN ASSERTION, NOT AN
// ACCIDENT. There is no branch, no prop and no schema shape that can produce
// one here — `SchemaInputFields` renders bounded integers and booleans and
// nothing else, and when it cannot read the schema the form says so and leaves
// Run disabled rather than falling back to free text. The paired test that
// renders every branch of this form and queries for a textbox is the thing
// standing between "the strategy is visible" and "the strategy is a coloured
// word above the same prompt box".
//
// EVERYTHING NUMERIC IS THE SERVER'S, VERBATIM. `max_output_tokens` on the
// three deployed deterministic agents is literally 1. It renders as 1. It is
// not rounded up to something that reads like a sensible budget, not hidden
// because it looks like a bug, and not annotated with the console's opinion of
// it: if that number is wrong it is wrong in the registry, and a console that
// quietly repairs it is a console that hides the defect. The same rule governs
// the plan's ordinals (the server's `ordinal`, never the array index) and the
// identity plate (the WORKFLOW's hash, never the Agent's).
//
// THE STRATEGY BADGE IS NOT HERE. It lives once, on the workspace mast. A
// deterministic surface that says "deterministic" twice on one screen is
// decoration, and decoration is what 012 rejected.

import { Fragment, useCallback, useMemo, useState } from "react";
import { HashChip } from "../primitives/HashChip";
import {
  SchemaInputFields,
  humanizeProperty,
  initialSchemaValues,
  readSchemaFields,
  schemaValuesJson,
  schemaValuesValid,
  type SchemaValue,
} from "./SchemaInputFields";
import type { AgentDetailView, AgentWorkflowProfile } from "./types";

export const RUN_LABEL = "Run";
export const TYPED_INPUT_UNAVAILABLE = "This workflow’s typed input is unavailable.";

/** One frozen input, rendered as the server sent it. Arrays become a readable
 * list because a JSON array literal is not a value a reader can check against a
 * county name; everything else keeps its exact scalar text. */
function frozenValueText(value: unknown): string {
  if (Array.isArray(value)) return value.map((item) => frozenValueText(item)).join(", ");
  if (value === null) return "null";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export function RunForm({
  agent,
  profile,
  onSubmit,
  busy = false,
  disabled = false,
  blockedReason = null,
  submitLabel = RUN_LABEL,
  idPrefix = "run",
}: {
  agent: AgentDetailView;
  profile: AgentWorkflowProfile;
  /** Receives the exact JSON body for `StartAgentRunRequest.input`. */
  onSubmit: (input: string) => void;
  busy?: boolean;
  disabled?: boolean;
  blockedReason?: string | null;
  submitLabel?: string;
  idPrefix?: string;
}) {
  const fields = useMemo(() => readSchemaFields(profile.input_schema), [profile.input_schema]);
  const [values, setValues] = useState<Record<string, SchemaValue>>(() =>
    fields === null ? {} : { ...initialSchemaValues(fields) },
  );
  const change = useCallback((name: string, value: SchemaValue) => {
    setValues((prev) => ({ ...prev, [name]: value }));
  }, []);

  const inputValid = fields !== null && schemaValuesValid(fields, values);
  const canRun = inputValid && !busy && !disabled && blockedReason === null;
  const frozen = Object.entries(profile.fixed_inputs);
  const plan = profile.fixed_plan;
  const budgets = agent.budgets;

  return (
    <div className="ag-runform">
      <section className="ag-runform__card" aria-label="Start a deterministic run">
        <header className="ag-runform__head">
          <h3>Start a run</h3>
          <span className="ag-runform__lede">
            The plan below is fixed by the Workflow profile before the run starts. The model cannot
            add, reorder, or skip a step.
          </span>
        </header>

        {/* The identity plate is the WORKFLOW's, not the Agent's: the hash a
            reader copies from here is `workflow_config_hash`, which is what
            pins the plan and the frozen inputs below it. */}
        <dl className="ag-plate" aria-label="Workflow profile">
          <div className="ag-plate__field">
            <dt>Workflow</dt>
            <dd>
              <span className="ne-mono">{profile.workflow_id}</span>
            </dd>
          </div>
          <div className="ag-plate__field">
            <dt>Version</dt>
            <dd>
              <span className="ne-mono">{profile.workflow_version}</span>
            </dd>
          </div>
          <div className="ag-plate__field">
            <dt>Profile hash</dt>
            <dd>
              <HashChip hash={profile.workflow_config_hash} label="Copy workflow profile hash" />
            </dd>
          </div>
          <div className="ag-plate__field">
            <dt>Model policy</dt>
            <dd>
              <span className="ne-mono">{agent.model_policy_id}</span>
            </dd>
          </div>
        </dl>

        <div className="ag-runform__grid">
          <div className="ag-runform__col">
            {plan.length === 0 ? (
              <>
                <h4 className="ag-runform__h">Fixed plan</h4>
                <p className="ag-runform__absent">
                  This profile served no ordered plan. Nothing here infers one.
                </p>
              </>
            ) : (
              <>
                <h4 className="ag-runform__h">Fixed plan · {plan.length} steps</h4>
                <ol className="ag-plan">
                  {plan.map((step) => (
                    <li className="ag-plan__step" key={step.step_id}>
                      {/* The server's `ordinal`, never the array index: a plan
                          served starting at 3 renders 3. */}
                      <span className="ag-plan__ord">{step.ordinal}</span>
                      <span className="ag-plan__action">{step.action}</span>
                      <span className="ag-plan__kind">
                        {step.kind}
                        {step.model_role === null ? null : ` · ${step.model_role}`}
                      </span>
                    </li>
                  ))}
                </ol>
              </>
            )}
          </div>

          <div className="ag-runform__col">
            <h4 className="ag-runform__h">Frozen inputs · at v{profile.workflow_version}</h4>
            {frozen.length === 0 ? (
              <p className="ag-runform__absent">
                This profile freezes no inputs of its own <span className="ne-absent">—</span>
              </p>
            ) : (
              <dl className="ag-frozen">
                {frozen.map(([name, value]) => (
                  <Fragment key={name}>
                    <dt>{humanizeProperty(name)}</dt>
                    <dd>{frozenValueText(value)}</dd>
                  </Fragment>
                ))}
              </dl>
            )}

            <h4 className="ag-runform__h">Your input</h4>
            {fields === null ? (
              // No schema, no fields, no guessed form — and Run stays disabled.
              // Nothing here infers an input the server did not serve.
              <p className="ag-runform__absent" role="status">
                {TYPED_INPUT_UNAVAILABLE} <span className="ne-absent">—</span>
              </p>
            ) : (
              <SchemaInputFields
                fields={fields}
                values={values}
                onChange={change}
                disabled={busy || disabled}
                idPrefix={idPrefix}
              />
            )}
          </div>
        </div>

        <div className="ag-runform__foot">
          {/* VERBATIM. The deployed deterministic agents serve
              `max_output_tokens: 1`, and it renders as 1 — not rounded up to
              something that reads like a sensible budget, not hidden because it
              looks like a bug, not annotated with the console's opinion of it.
              If that number is wrong it is wrong in the registry. */}
          <p className="ag-budgets" aria-label="Budgets">
            <span>Budgets</span>
            <b>{budgets.max_steps.toLocaleString("en-US")}</b>{" "}
            {budgets.max_steps === 1 ? "step" : "steps"} ·{" "}
            <b>{budgets.max_tool_calls.toLocaleString("en-US")}</b> tool calls ·{" "}
            <b>{budgets.max_input_tokens.toLocaleString("en-US")}</b> in ·{" "}
            <b>{budgets.max_output_tokens.toLocaleString("en-US")}</b> out
          </p>
          <button
            type="button"
            className="ne-btn ne-btn--primary"
            disabled={!canRun}
            onClick={() => {
              if (!canRun || fields === null) return;
              onSubmit(schemaValuesJson(fields, values));
            }}
          >
            {busy ? "Submitting…" : submitLabel}
          </button>
          {blockedReason === null ? null : (
            <p className="ag-runform__absent" role="status">
              Unavailable — {blockedReason}
            </p>
          )}
        </div>
      </section>
    </div>
  );
}
