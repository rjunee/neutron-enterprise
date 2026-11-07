// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// REDESIGN §1.4 — the INSPECT switch. A LABELLED control with a VISIBLE state
// word, never an icon: it must be discoverable in the first thirty seconds,
// because it is the single mechanism that lets one console serve an ops leader
// at a glance and a platform architect under inspection.

import { useInspect } from "./InspectContext";

export function InspectSwitch() {
  const { inspect, toggle } = useInspect();
  return (
    <button
      type="button"
      className={`ne-inspect${inspect ? " ne-inspect--on" : ""}`}
      role="switch"
      aria-checked={inspect}
      onClick={toggle}
      title="Open every evidence drawer on the page. Keyboard: I"
    >
      <span className="ne-inspect__track" aria-hidden="true">
        <span className="ne-inspect__knob" />
      </span>
      <span className="ne-inspect__word">INSPECT {inspect ? "ON" : "OFF"}</span>
    </button>
  );
}
