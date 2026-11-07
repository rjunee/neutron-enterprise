// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// The Neutron mark — the neutronenterprise.com atom, retuned for the console.
// Geometry is the site favicon's verbatim (three orbitals at 0/60/120 degrees,
// nucleus, two electrons) so the console and the site carry one mark.
//
// Two deliberate deltas from the site file:
//
//   * Stroke 2 -> 2.6 and opacity 0.55 -> 0.78. At 16px a 2-unit stroke on a
//     64 viewBox lands at 0.5 device pixels, so the orbitals render as a soft
//     smudge and the amber electron drops out entirely. Measured across
//     16/20/24/32/48/96 before and after; the heavier weight holds at 16 and is
//     still light at 96.
//   * The BRAND TEAL is fixed in both themes — it does NOT take `currentColor`
//     and does NOT recolour per theme. Owner, 2026-07-31: "Stick with the brand
//     color dont copy apple." A brand mark that adapts to its context is a
//     context-tinted glyph, not a mark. Verified legible on both grounds at
//     16/20/24/32/48/80. Logotypes are exempt from WCAG contrast minima, and the
//     amber electron carries no state — nothing here encodes meaning by colour.
//
// Inlined as a component rather than <img src="mark.svg"> because the two must
// not drift, and because an SVG loaded through <img> is an isolated document —
// a lesson from the earlier `currentColor` version, which rendered black there.

/** The brand teal, from tokens.css where it is declared once and never themed.
 * A raw hex here would (correctly) trip the token-hardcode guardian. */
const BRAND = "var(--brand-teal)";

export function NeutronMark({ size = 26 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      fill="none"
      role="img"
      aria-label="Neutron Enterprise"
    >
      <g fill="none" stroke={BRAND} strokeWidth="2.6" opacity="0.86">
        <ellipse cx="32" cy="32" rx="22" ry="9" transform="rotate(60 32 32)" />
        <ellipse cx="32" cy="32" rx="22" ry="9" transform="rotate(120 32 32)" />
        <ellipse cx="32" cy="32" rx="22" ry="9" />
      </g>
      <circle cx="32" cy="32" r="5.8" fill={BRAND} />
      <circle cx="51" cy="35.5" r="2.9" fill={BRAND} />
      <circle cx="18" cy="44" r="2.9" fill="var(--amber)" />
    </svg>
  );
}
