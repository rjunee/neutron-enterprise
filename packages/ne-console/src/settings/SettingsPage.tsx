// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// §16 — SETTINGS. Two kinds of setting, kept visibly apart: what this PERSON
// prefers (local, instant) and what this DEPLOYMENT is configured to do
// (governed, approved). Mixing them is how a theme toggle ends up looking like
// it changed something about the deployment.
//
// WHAT THIS REPLACES: a heading over nothing.
//
// YOUR PREFERENCES IS FULLY BUILT AND FULLY LIVE. Theme and density write the
// SAME state the account menu writes (rail/theme.tsx, primitives/density.tsx),
// so the two surfaces cannot disagree about either, and the guided tour reopens
// through the same summon the account menu calls.
//
// DEPLOYMENT IS READ-ONLY, and says so in one caption. The two writable fields
// live in the configuration this deployment already serves; §18 B7 makes them
// editable with the §2.5 pending pattern, and that path is built here behind the
// same probe §10 uses — the fields become inputs, the submit names its
// consequence, and the caption that would then be false goes away. Nothing else
// from the configuration is surfaced: Settings is not a viewer for values that
// have no governed write behind them.
//
// THIN CLIENT (C7). Every deployment value is a served field. An absent one
// renders the em dash and a short factual note — never a default this console
// picked and presented as the deployment's answer.

import { useCallback, useContext, useMemo, useState } from "react";
import type { ReactNode } from "react";
import { ApiError } from "../api/client";
import { RuntimeConfigContext } from "../AppBootstrap";
import { useApiClient } from "../auth/useApiClient";
import { useDemo } from "../demo/DemoContext";
import { useGovernedRead } from "../landing/useGovernedRead";
import { ListScreen } from "../primitives/PageHeader";
import {
  Loading,
  LoadError,
  PendingMarker,
  PolicyDenied,
  Refused,
  SaveError,
} from "../primitives/states";
import { PrimaryButton, ToolbarButton } from "../primitives/Toolbar";
import { useDensity } from "../primitives/density";
import type { Density } from "../primitives/density";
import { formatDuration } from "../primitives/format";
import { useTheme } from "../rail/theme";
import type { ThemeChoice } from "../rail/theme";
import { isEmptyPatch, secondsField, secondsToMs, settingsPatch } from "./settings";
import { settingsReader, settingsWriter } from "./reads";
import type { SettingsWriter } from "./reads";
import "./settings.css";

const THEME_OPTIONS: ReadonlyArray<{ value: ThemeChoice; label: string }> = [
  { value: "dark", label: "Dark" },
  { value: "light", label: "Light" },
  { value: "system", label: "System" },
];

const DENSITY_OPTIONS: ReadonlyArray<{ value: Density; label: string }> = [
  { value: "compact", label: "Compact" },
  { value: "comfortable", label: "Comfortable" },
];

// ===========================================================================
// Layout
// ===========================================================================

function Section({ title, id, children }: { title: string; id: string; children: ReactNode }) {
  return (
    <section className="ne-settings__section" aria-labelledby={id}>
      <h2 className="ne-settings__title" id={id}>
        {title}
      </h2>
      {children}
    </section>
  );
}

/** One labelled row. The label is a real <dt> and the value a real <dd>, so the
 * pair is announced as a pair rather than as two loose strings. */
function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="ne-settings__row">
      <dt className="ne-settings__label">{label}</dt>
      <dd className="ne-settings__value">{children}</dd>
    </div>
  );
}

/** An absent configured value: the em dash plus the short factual reason
 * (§2.1's column resilience, applied to a field). Never a default this console
 * chose, presented as the deployment's. */
function NotSet() {
  return (
    <>
      <span className="ne-absent">—</span> <span className="ne-settings__note">Not set</span>
    </>
  );
}

// ===========================================================================
// Your preferences — local, instant
// ===========================================================================

function Radios<T extends string>({
  legend,
  name,
  options,
  value,
  onChange,
}: {
  legend: string;
  name: string;
  options: ReadonlyArray<{ value: T; label: string }>;
  value: T;
  onChange: (next: T) => void;
}) {
  return (
    <fieldset className="ne-settings__radios">
      <legend className="ne-settings__legend">{legend}</legend>
      {options.map((option) => (
        <label className="ne-settings__radio" key={option.value}>
          <input
            type="radio"
            name={name}
            checked={value === option.value}
            onChange={() => onChange(option.value)}
          />
          <span>{option.label}</span>
        </label>
      ))}
    </fieldset>
  );
}

function Preferences() {
  const { theme, setTheme } = useTheme();
  const { density, setDensity } = useDensity();
  const { summon } = useDemo();
  return (
    <Section title="Your preferences" id="ne-settings-preferences">
      <div className="ne-settings__prefs">
        <Radios
          legend="Theme"
          name="ne-settings-theme"
          options={THEME_OPTIONS}
          value={theme}
          onChange={setTheme}
        />
        <Radios
          legend="Density"
          name="ne-settings-density"
          options={DENSITY_OPTIONS}
          value={density}
          onChange={setDensity}
        />
        <div className="ne-settings__tour">
          <span className="ne-settings__legend">Guided tour</span>
          <ToolbarButton label="Reopen the guided tour" onClick={summon} />
        </div>
      </div>
    </Section>
  );
}

// ===========================================================================
// Deployment — governed
// ===========================================================================

type WriteState =
  | { kind: "idle" }
  | { kind: "saving" }
  /** 403: the server's reason, rendered verbatim where the submit happened —
   * or null where it stated none, because the console does not write one on
   * the gate's behalf. */
  | { kind: "denied"; reason: string | null }
  | { kind: "error"; detail: string | null };

function WriteOutcome({ state }: { state: WriteState }) {
  if (state.kind === "denied") return <PolicyDenied reason={state.reason} />;
  if (state.kind === "error") return <SaveError detail={state.detail} />;
  return null;
}

/** The service address is the API base in force. It is read-only on every
 * deployment and on every path — B7 does not make it writable, so it is never
 * offered as a field. */
function ServiceAddress({ apiBase }: { apiBase: string | null }) {
  return <Row label="Service address">{apiBase === null ? <NotSet /> : apiBase}</Row>;
}

/**
 * The v1 Deployment section: read-only, from the configuration this deployment
 * serves, with §16's one caption.
 */
function DeploymentConfigured({
  name,
  deadlineMs,
  apiBase,
}: {
  name: string | null;
  deadlineMs: number | null;
  apiBase: string | null;
}) {
  return (
    <>
      <dl className="ne-settings__fields">
        <Row label="Deployment name">{name === null ? <NotSet /> : name}</Row>
        <Row label="Search time limit">
          {deadlineMs === null ? <NotSet /> : formatDuration(deadlineMs)}
        </Row>
        <ServiceAddress apiBase={apiBase} />
      </dl>
      <p className="ne-settings__caption">Managed in this deployment&rsquo;s configuration.</p>
    </>
  );
}

/**
 * The §18 B7 Deployment section: the two fields served by the settings read,
 * editable, submitted through the governed path.
 *
 * §2.5 to the letter — the OLD VALUE REMAINS THE DISPLAYED VALUE while a change
 * is parked at the gate. The pending marker is read back from the server rather
 * than remembered here, which is the only way it survives a reload, and nothing
 * is written into local state on either applied or pending: both re-read.
 */
function DeploymentGoverned({
  apiBase,
  writer,
  read,
}: {
  apiBase: string | null;
  writer: SettingsWriter | null;
  read: ReturnType<typeof useDeploymentRead>;
}) {
  const served = read.data;
  const [name, setName] = useState<string | null>(null);
  const [seconds, setSeconds] = useState<string | null>(null);
  const [state, setState] = useState<WriteState>({ kind: "idle" });

  if (read.status === "loading") return <Loading things="deployment settings" />;
  if (read.status === "refused") return <Refused thing="deployment settings" page="Settings" />;
  if (read.status === "error" || served === null) {
    return <LoadError things="deployment settings" onRetry={read.reload} />;
  }

  // The edited text falls back to the SERVED value until the person types, so a
  // reload that brings a new server value is not overwritten by a stale draft.
  const nameField = name ?? served.deployment_name ?? "";
  const secondsText = seconds ?? secondsField(served.search_deadline_ms);
  const patch = settingsPatch(served, {
    deployment_name: nameField,
    search_deadline_ms: secondsToMs(secondsText),
  });
  const busy = state.kind === "saving";

  async function submit() {
    if (writer === null) return;
    setState({ kind: "saving" });
    try {
      await writer(patch);
      setState({ kind: "idle" });
      setName(null);
      setSeconds(null);
      read.reload();
    } catch (err: unknown) {
      if (err instanceof ApiError && err.status === 403) {
        // THE GATE'S OWN SENTENCE OR NOTHING: `PolicyDenied` renders this as
        // the gate's account of the denial, so a 403 with no served `detail`
        // states the denial alone rather than a console sentence signed with
        // the gate's name.
        setState({ kind: "denied", reason: err.detail ?? null });
        return;
      }
      setState({ kind: "error", detail: err instanceof ApiError ? (err.detail ?? null) : null });
    }
  }

  return (
    <>
      <dl className="ne-settings__fields">
        <Row label="Deployment name">
          {writer === null ? (
            served.deployment_name === null ? (
              <NotSet />
            ) : (
              served.deployment_name
            )
          ) : (
            <input
              type="text"
              className="ne-settings__input"
              aria-label="Deployment name"
              value={nameField}
              onChange={(event) => setName(event.target.value)}
            />
          )}
        </Row>
        <Row label="Search time limit">
          {writer === null ? (
            served.search_deadline_ms === null ? (
              <NotSet />
            ) : (
              formatDuration(served.search_deadline_ms)
            )
          ) : (
            <span className="ne-settings__unit">
              <input
                type="number"
                min="1"
                className="ne-settings__input ne-settings__input--num"
                aria-label="Search time limit in seconds"
                value={secondsText}
                onChange={(event) => setSeconds(event.target.value)}
              />
              <span>seconds</span>
            </span>
          )}
        </Row>
        <ServiceAddress apiBase={apiBase} />
      </dl>
      {served.pending_change === null ? null : (
        <PendingMarker
          by={served.pending_change.requested_by}
          requestId={served.pending_change.request_id}
        />
      )}
      {writer === null ? null : (
        <div className="ne-settings__submit">
          <PrimaryButton
            label="Request change"
            disabled={busy || isEmptyPatch(patch)}
            onClick={() => void submit()}
          />
        </div>
      )}
      <WriteOutcome state={state} />
    </>
  );
}

/** The B7 read, or a resolved empty one on a deployment without it. Declared as
 * its own hook so the component above can name its type without the screen
 * calling a hook conditionally. */
function useDeploymentRead() {
  const client = useApiClient();
  const reader = useMemo(() => settingsReader(client), [client]);
  return useGovernedRead(
    useCallback(async () => (reader === null ? null : await reader()), [reader]),
  );
}

// ===========================================================================
// The screen
// ===========================================================================

export function SettingsPage() {
  const client = useApiClient();
  // A NULLABLE read of the runtime config: this page renders inside the
  // bootstrapped shell, but it must not be the thing that throws if it ever
  // does not.
  const config = useContext(RuntimeConfigContext);
  const reader = useMemo(() => settingsReader(client), [client]);
  const writer = useMemo(() => settingsWriter(client), [client]);
  const read = useDeploymentRead();

  return (
    <div className="ne-settings">
      <ListScreen title="Settings">
        <Preferences />
        <Section title="Deployment" id="ne-settings-deployment">
          {reader === null ? (
            <DeploymentConfigured
              name={config?.deployment_name ?? null}
              deadlineMs={config?.search_deadline_ms ?? null}
              apiBase={config?.api_base ?? null}
            />
          ) : (
            <DeploymentGoverned apiBase={config?.api_base ?? null} writer={writer} read={read} />
          )}
        </Section>
      </ListScreen>
    </div>
  );
}
