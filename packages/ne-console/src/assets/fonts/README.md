# Self-hosted fonts (NE-130)

These woff2 files are **vendored, committed static assets** — served locally via
`@font-face` in `../../styles/fonts.css` with `font-display: block`. The running
console makes **zero external font requests** (design §1.2, C1/C2).

They are fetched at build time by `../../../scripts/vendor-fonts.mjs` from pinned,
immutable, version-locked jsDelivr URLs of the `@fontsource` package files.
**No `@fontsource` package is added to `package.json`** — OFL-1.1 is not in the
npm license policy's free list (and that policy file is out of this unit's lane),
so the fonts stay out of the npm closure and the license gate is unaffected.

To re-vendor (reproducible): `node scripts/vendor-fonts.mjs`.

## The set

Source Sans 3 (UI, weights 400/500/600/700) + Fira Mono (data, weights 400/500/700)
— the round-3 pairing decided in `../../../design/round3/type/RECOMMENDATION.md`,
whose `fonts.json` carries the same per-file byte counts and sha256 recorded below.
Static per-weight faces only: nothing here is variable and nothing carries a width
axis. Total **113,728 bytes**.

## Provenance

All faces are **SIL Open Font License 1.1** (OFL-1.1). License texts are committed
alongside as `OFL-Source-Sans-3.txt` and `OFL-Fira-Mono.txt` and recorded in
`packages/ne-console/NOTICE`.

| File | Bytes | Upstream project | fontsource package@version | sha256 |
|---|---|---|---|---|
| `source-sans-3-latin-400-normal.woff2` | 15,696 | [Source Sans](https://github.com/adobe-fonts/source-sans) (Adobe) | `@fontsource/source-sans-3@5.3.0` | `0f73f35e08cde0a2f10c109c6e01d71459d97e4099ecd9a50f1b6c0209e4de2b` |
| `source-sans-3-latin-500-normal.woff2` | 15,652 | [Source Sans](https://github.com/adobe-fonts/source-sans) | `@fontsource/source-sans-3@5.3.0` | `3b3a8b8e4a422ff71c9ffb0836a4d48ff337a4419b878214f65d6ebe0f59fa51` |
| `source-sans-3-latin-600-normal.woff2` | 15,668 | [Source Sans](https://github.com/adobe-fonts/source-sans) | `@fontsource/source-sans-3@5.3.0` | `14527d193b0e30bc32ef931549a246cdfd286573bb12869b7e052b8101a39d38` |
| `source-sans-3-latin-700-normal.woff2` | 15,596 | [Source Sans](https://github.com/adobe-fonts/source-sans) | `@fontsource/source-sans-3@5.3.0` | `ae35266f2ab0d5314ff9a04127931eb26c793e4f1269bb240046d4d2db4b7bb1` |
| `fira-mono-latin-400-normal.woff2` | 16,640 | [Fira Mono](https://github.com/mozilla/Fira) (Mozilla) | `@fontsource/fira-mono@5.3.0` | `8d1e753569ea7964a3beacda126450f8469b70c14c161ae8062cf4c12835e947` |
| `fira-mono-latin-500-normal.woff2` | 16,480 | [Fira Mono](https://github.com/mozilla/Fira) | `@fontsource/fira-mono@5.3.0` | `291f90e3f0baf1f357ece7b82634b8bcd6c080def7648694894548af3d28af26` |
| `fira-mono-latin-700-normal.woff2` | 17,996 | [Fira Mono](https://github.com/mozilla/Fira) | `@fontsource/fira-mono@5.3.0` | `f08b65965da9f065bea1bb354a374fe6b19e100904861840c0bd5d6b2dc79c93` |

| License file | Source | sha256 |
|---|---|---|
| `OFL-Source-Sans-3.txt` | `@fontsource/source-sans-3@5.3.0/LICENSE` | `18aabf190848725e2576eefb5c29ba06aac1029d02132252a7f312eac2e50cf3` |
| `OFL-Fira-Mono.txt` | `@fontsource/fira-mono@5.3.0/LICENSE` | `18aabf190848725e2576eefb5c29ba06aac1029d02132252a7f312eac2e50cf3` |

Both license files hash identically: OFL-1.1 is a fixed text and the two packages
ship it byte-for-byte the same. They are committed separately so each family
carries its own licence alongside it.
