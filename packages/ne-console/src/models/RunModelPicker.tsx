// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

import { useCallback, useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import type {
  AgentModelCandidateWire,
  ModelChoiceCatalogView,
  ModelChoiceSurface,
} from "../api/types";
import { useApiClient } from "../auth/useApiClient";
import { useLatestRequest } from "../hooks/useLatestRequest";

type ChoiceState =
  | { kind: "loading"; client: object; surface: ModelChoiceSurface; generation: number }
  | { kind: "error"; client: object; surface: ModelChoiceSurface; generation: number }
  | {
      kind: "ready";
      client: object;
      surface: ModelChoiceSurface;
      generation: number;
      catalog: ModelChoiceCatalogView;
    };

const REFRESH_MS = 30_000;

/** One picker shared by every model-planned run surface.
 *
 * Its options are exactly the server's current catalog. A failed read leaves no
 * prior success rendered, and the shared latest-request guard prevents an older
 * response from replacing a newer surface's catalog. `observed_at` is retained
 * as server evidence when the server measured any, and is deliberately not
 * turned into a browser age/timer.
 *
 * The remote catalog for a run surface is the CONFIGURED model list, so an
 * option here is an offer, not a promise: the server settles availability at
 * submit and refuses by name. The picker must therefore never present a
 * selectable option as proven-live, and never quietly change the user's pick.
 */
interface SharedPickerProps {
  value: string | null;
  onChange: (model: string | null) => void;
  onReadinessChange?: (ready: boolean) => void;
  disabled?: boolean;
  disabledReason?: string;
  label?: string;
}

type RunModelPickerProps = SharedPickerProps &
  (
    | {
        surface: ModelChoiceSurface;
        catalog?: never;
        invalidationKey?: number;
      }
    | {
        /** Exact Agent-specific candidates returned by GET /agents/{id}/preflight. */
        catalog: {
          readonly choices: readonly AgentModelCandidateWire[];
          readonly effectiveDefault: string | null;
        };
        surface?: never;
        invalidationKey?: never;
      }
  );

export function RunModelPicker(props: RunModelPickerProps) {
  if (props.catalog !== undefined) return <CatalogModelPicker {...props} />;
  return <RemoteModelPicker {...props} />;
}

function RemoteModelPicker({
  surface,
  value,
  onChange,
  onReadinessChange,
  disabled = false,
  disabledReason,
  label = "Run model",
  invalidationKey = 0,
}: SharedPickerProps & {
  surface: ModelChoiceSurface;
  /** Advanced by the owning run surface when the server definitively rejects
   * the selected alias. The parent clears `value` in that same event; this key
   * hides the stale catalog and prevents an older in-flight read restoring it. */
  invalidationKey?: number;
}) {
  const client = useApiClient();
  const runLatest = useLatestRequest();
  const id = useId();
  const generationRef = useRef(invalidationKey);
  const valueRef = useRef(value);
  // What THIS picker last announced, which is not the same thing as the `value`
  // prop. An owner that opens a new ownership generation drops the alias it was
  // holding and re-renders with `value === null` before we are asked to
  // re-announce, so the prop mirror above is already gone by then. This ref is
  // the picker's own record and survives that, which is what lets the alias be
  // handed to the new generation instead of a spurious null.
  const announcedRef = useRef<string | null>(null);
  // The owner may hand us a NEW pair of callbacks, and for ChatPage/Workflows
  // that is a signal, not noise: their setters are scoped to an ownership
  // generation (`useCallback([owner])`) and the selection they hold is dropped
  // when that generation changes — notably when `/me` settles for the SAME
  // client. Holding the callbacks in refs keeps `requestChoices` stable so the
  // change no longer triggers a second read; the re-report effect below carries
  // the signal instead, by re-announcing the catalog we already have.
  const onChangeRef = useRef(onChange);
  const onReadinessChangeRef = useRef(onReadinessChange);
  useLayoutEffect(() => {
    onChangeRef.current = onChange;
    onReadinessChangeRef.current = onReadinessChange;
  });
  useLayoutEffect(() => {
    valueRef.current = value;
  }, [value]);
  useLayoutEffect(() => {
    generationRef.current = invalidationKey;
    onReadinessChangeRef.current?.(false);
  }, [invalidationKey]);
  const [state, setState] = useState<ChoiceState>({
    kind: "loading",
    client,
    surface,
    generation: invalidationKey,
  });

  const requestChoices = useCallback(() => {
    const generation = generationRef.current;
    void runLatest(() => client.modelChoices(surface)).then((outcome) => {
      if (outcome.kind === "superseded" || generation !== generationRef.current) return;
      if (outcome.kind === "error") {
        valueRef.current = null;
        announcedRef.current = null;
        onChangeRef.current(null);
        onReadinessChangeRef.current?.(false);
        setState({ kind: "error", client, surface, generation });
        return;
      }
      setState({ kind: "ready", client, surface, generation, catalog: outcome.value });
      const effectiveDefault = outcome.value.effective_default;
      const hasEffectiveDefault =
        effectiveDefault !== null &&
        outcome.value.choices.some((choice) => choice.model === effectiveDefault);
      const retained = outcome.value.choices.some((choice) => choice.model === valueRef.current)
        ? valueRef.current
        : null;
      const selected = retained ?? (hasEffectiveDefault ? effectiveDefault : null);
      valueRef.current = selected;
      announcedRef.current = selected;
      onChangeRef.current(selected);
      onReadinessChangeRef.current?.(selected !== null);
    });
  }, [client, runLatest, surface]);

  // ONE automatic read per (client, surface). `requestChoices` no longer
  // changes identity when the owner re-renders, so the Ask page stopped reading
  // this endpoint twice per load — once before `/me` settled and once after.
  //
  // `invalidationKey` is deliberately NOT a dependency: a server invalidation
  // must leave the user on the "Model choices changed" prompt and re-read only
  // when they ask, so a stale list is never silently re-offered.
  useEffect(() => {
    // The state is surface-tagged, so render switches to loading immediately
    // on a surface change without a synchronously cascaded local state update.
    valueRef.current = null;
    announcedRef.current = null;
    onChangeRef.current(null);
    onReadinessChangeRef.current?.(false);
    requestChoices();
  }, [client, requestChoices, surface]);

  useEffect(() => {
    const interval = window.setInterval(requestChoices, REFRESH_MS);
    return () => window.clearInterval(interval);
  }, [requestChoices]);

  // Whom the current selection was last announced to.
  const reportedToRef = useRef<{
    onChange: (model: string | null) => void;
    onReadinessChange: ((ready: boolean) => void) | undefined;
  } | null>(null);

  useEffect(() => {
    const previous = reportedToRef.current;
    reportedToRef.current = { onChange, onReadinessChange };
    if (previous === null) return; // The mount read announces itself.
    if (previous.onChange === onChange && previous.onReadinessChange === onReadinessChange) return;
    // New callbacks mean the owner opened a new ownership generation and let go
    // of the alias it was holding. Re-announce what we already read: this
    // generation change (a settling `/me` for the same client) does not make
    // the catalog untrue, so re-reading it would be exactly the duplicate load
    // this component no longer performs. A genuinely different client changes
    // `client` too, and the read effect above refetches for that.
    const selected = announcedRef.current;
    onChange(selected);
    onReadinessChange?.(selected !== null);
  }, [onChange, onReadinessChange]);

  const load = useCallback(() => {
    // A user-requested retry clears both the selection and visible evidence in
    // the same event before issuing the replacement request.
    valueRef.current = null;
    announcedRef.current = null;
    onChangeRef.current(null);
    onReadinessChangeRef.current?.(false);
    setState({ kind: "loading", client, surface, generation: generationRef.current });
    requestChoices();
  }, [client, requestChoices, surface]);

  const invalidated =
    state.client === client && state.surface === surface && state.generation !== invalidationKey;
  if (invalidated) {
    return (
      <span>
        <span role="alert">Model choices changed. Refresh to continue.</span>{" "}
        <button type="button" className="ne-btn ne-btn--quiet" onClick={load}>
          Retry
        </button>
      </span>
    );
  }

  const visibleState: ChoiceState =
    state.client === client && state.surface === surface
      ? state
      : { kind: "loading", client, surface, generation: invalidationKey };

  if (visibleState.kind === "loading") {
    return <span role="status">Loading model choices…</span>;
  }
  if (visibleState.kind === "error") {
    return (
      <span>
        <span role="alert">Couldn’t load model choices.</span>{" "}
        <button type="button" className="ne-btn ne-btn--quiet" onClick={load}>
          Retry
        </button>
      </span>
    );
  }
  const hasSelectedChoice =
    value !== null && visibleState.catalog.choices.some((choice) => choice.model === value);
  const hasEffectiveDefault =
    visibleState.catalog.effective_default !== null &&
    visibleState.catalog.choices.some(
      (choice) => choice.model === visibleState.catalog.effective_default,
    );
  if (!hasSelectedChoice && !hasEffectiveDefault) {
    return <span role="status">No model is currently available for this run.</span>;
  }

  return (
    <span className="ne-filter">
      <label className="ne-filter__label" htmlFor={id}>
        {label}
      </label>
      <select
        id={id}
        className="ne-filter__select"
        value={value ?? ""}
        disabled={disabled}
        onChange={(event) => {
          announcedRef.current = event.target.value;
          onChange(event.target.value);
        }}
      >
        {visibleState.catalog.choices.map((choice) => (
          <option key={choice.model} value={choice.model}>
            {choice.model}
          </option>
        ))}
      </select>
      {disabled && disabledReason ? <span role="status">{disabledReason}</span> : null}
    </span>
  );
}

function CatalogModelPicker({
  catalog,
  value,
  onChange,
  onReadinessChange,
  disabled = false,
  disabledReason,
  label = "Run model",
}: SharedPickerProps & {
  catalog: {
    readonly choices: readonly AgentModelCandidateWire[];
    readonly effectiveDefault: string | null;
  };
}) {
  const id = useId();
  const selectedExists = catalog.choices.some((choice) => choice.model === value);
  const defaultExists = catalog.choices.some((choice) => choice.model === catalog.effectiveDefault);
  const selected = selectedExists ? value : defaultExists ? catalog.effectiveDefault : null;

  useEffect(() => {
    // An omitted value means "use the server effective default" and does not
    // need a client-authored selection. Only clear/replace an explicit value
    // that the new server catalog no longer contains.
    if (value !== null && value !== selected) onChange(selected);
    onReadinessChange?.(selected !== null);
  }, [onChange, onReadinessChange, selected, value]);

  if (selected === null) {
    return <span role="status">No model is currently available for this run.</span>;
  }
  return (
    <span className="ne-filter">
      <label className="ne-filter__label" htmlFor={id}>
        {label}
      </label>
      <select
        id={id}
        className="ne-filter__select"
        value={selected}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
      >
        {catalog.choices.map((choice) => (
          <option key={`${choice.model}:${choice.lane}`} value={choice.model}>
            {`${choice.model} · ${choice.provider} · ${choice.lane}`}
          </option>
        ))}
      </select>
      {disabled && disabledReason ? <span role="status">{disabledReason}</span> : null}
    </span>
  );
}
