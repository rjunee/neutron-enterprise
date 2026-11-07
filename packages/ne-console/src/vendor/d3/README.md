<!-- Copyright (c) 2026 Ryan Junee.
     SPDX-License-Identifier: Apache-2.0 -->

# Vendored d3 (Graph Chamber physics)

The Graph Chamber (NE-134) lays out the governed T2 knowledge subgraph with a
force simulation. We **vendor** the d3-force ES-module source (and its three
sibling packages it imports) as **committed static sources** under this
directory rather than adding them to `package.json`/`package-lock.json`.

**Runtime makes ZERO external requests** — these are committed static sources
bundled by Vite from local disk (C1/C2). The fetch happens **only at vendor
time** via `scripts/vendor-d3.mjs` (plain node, zero deps, lives outside `src/`).
Re-run that script to re-vendor; it re-prints every sha256 below.

## Why vendored (not an npm dep)

The fonts precedent: keeping these OUT of the npm closure means the npm license
gate's inputs (`package-lock.json`) are **unchanged** by this unit — `git diff`
shows no `package.json`/`package-lock.json` change. The license audit trail lives
here instead (C4/C11): full ISC texts at `LICENSE-<pkg>`, provenance + sha256
below, and a NOTICE entry in `packages/ne-console/NOTICE`.

## Packages & versions (pinned; re-verified 2026-07-11, all ISC)

| package      | version | license | upstream (jsDelivr `src/`)                                  |
| ------------ | ------- | ------- | ----------------------------------------------------------- |
| d3-force     | 3.0.0   | ISC     | https://cdn.jsdelivr.net/npm/d3-force@3.0.0/src/             |
| d3-quadtree  | 3.0.1   | ISC     | https://cdn.jsdelivr.net/npm/d3-quadtree@3.0.1/src/          |
| d3-dispatch  | 3.0.1   | ISC     | https://cdn.jsdelivr.net/npm/d3-dispatch@3.0.1/src/          |
| d3-timer     | 3.0.1   | ISC     | https://cdn.jsdelivr.net/npm/d3-timer@3.0.1/src/             |

Design §8 named only `d3-force (+d3-quadtree)`. d3-force's `simulation.js` also
imports `d3-dispatch` and `d3-timer` (same author, same ISC license), so all
FOUR are vendored — additive to §8's list, flagged in the NE-134 build brief's
`componentVerification`. ISC is functionally MIT-equivalent (C4 "free to embed").

## Import rewrites (EXACTLY three targets, deterministic)

The committed sources resolve their sibling packages by **relative** path (no
bundler bare-specifier resolution, no package.json entry). `scripts/vendor-d3.mjs`
applies exactly these rewrites and fails loudly if a target is missing:

- `d3-force/simulation.js`: `"d3-dispatch"` → `"../d3-dispatch/index.js"`,
  `"d3-timer"` → `"../d3-timer/index.js"`
- `d3-force/collide.js`: `"d3-quadtree"` → `"../d3-quadtree/index.js"`
- `d3-force/manyBody.js`: `"d3-quadtree"` → `"../d3-quadtree/index.js"`

## Types

`d3-force/index.d.ts` is **hand-written by us** (copyright header, not vendored)
declaring only the subset of the API `src/knowledge/chamberSim.ts` uses. `tsc`
uses it as the type surface over the vendored `.js`; no `@types/d3-force` enters
the npm closure. The other three packages need no declarations (d3-force's public
API is the only surface we import).

## Determinism

d3-force v3 uses a built-in LCG (`lcg.js`) as its random source, so the layout is
reproducible across runs — `chamberSim.test.ts` relies on this.

## sha256 provenance (every vendored file; sources post-rewrite where noted)

```
d3-force/center.js       8f1e92cb0bdaf2820379470528e8e47d58d7521175885215719de280514f474e
d3-force/collide.js      31471216852593e53fc2bb1248a39e990d017b425d9c08909ba9ba965c74dadd  (rewritten)
d3-force/constant.js     88f2653180d441d431ad68e0e2cf2c02aea72976c5ff24b6ed988a5bb2b78847
d3-force/index.js        0b923d420d49ae80d5d3578de4ac45645adad85254eefc5ca67f98112993e8e6
d3-force/jiggle.js       26641b82cae7d49d6eb865c4d9b66a6af2eb3021705a0b80af737a4883cc494a
d3-force/lcg.js          7e1aaa1836a35ab82c6153c88342b3e61933336d4f6b1e55e82fda13ac23e5df
d3-force/link.js         c90230afc1a230e30ef9b355eb4ab0b9c92daf0bcf816fa3dbc1b9820ef6b441
d3-force/manyBody.js     9a1cf92e7dae8289e1eaeb8cf231f9d1e96ee0930c2f06d35179613545548832  (rewritten)
d3-force/radial.js       7dd44227f14c6d855719fd4d1bec3a3d3b30d402f7e42138e9bf4dfebe07318b
d3-force/simulation.js   873ff146b002e8bca065f903e279241d505e7eaf810bda209d8dd55af2bb0e62  (rewritten)
d3-force/x.js            840c6a9f3320d3d5acb1e7f17626cdc9ff8ef6bde16870f20df7b682be17af17
d3-force/y.js            c3a544d39809f8eedff7bcbac7fd02903f1b5c6418b01d9d0cbcbf1199c887c3
d3-quadtree/add.js       22573f1b407d07a4b10a0641767d66d4ec6593d5f5accc0a050d7c6b0a9742f1
d3-quadtree/cover.js     9ca4bcd47632a10017853c48c13592866e06fff9bed0dbe0f7bd7633ecd755f0
d3-quadtree/data.js      7f461ec1d885f744dda7bce9e713b954d96c7dd40b060f47fa469b9e1e6d46bb
d3-quadtree/extent.js    5a915972377166682524e6502f592eeae3a9812ea8c004011b5002be544875bb
d3-quadtree/find.js      f531172551d5ca03c26b54cab8e69bc5b50df46b22de9287e4210915ed584063
d3-quadtree/index.js     6f668c1f65bf1548739c8a12e419e6968f4868d2a2916a74799451b4d753bdaf
d3-quadtree/quad.js      2625cdc52b52686d9a29d11901206df6133c719c3efce41e505b99018e3e9064
d3-quadtree/quadtree.js  90af25659262d4a171a2d0ad54894c03c2ef2cbb24168ce52b7ef201ff73acd4
d3-quadtree/remove.js    d9d89cf2ec170f8a50dfcc3620c07dc39a40a523dac71ed7b833363de704c6a1
d3-quadtree/root.js      0de405a6a916a8cb30fa4f0c2b9b7315e3a13f82235133871d0903fb076f8fc4
d3-quadtree/size.js      f5ae7a15c65da057c1805a08c0f88f751b995b3abf39416be9db26bbdb116c9b
d3-quadtree/visit.js     c33e8b439b8031fc4c9137b7464f5960004353e601522426e92afb63090397f7
d3-quadtree/visitAfter.js a9520ccb4075d3096605f86f5df3e0f5d476f89df236ea00921cb5ef05357a66
d3-quadtree/x.js         eab8b4839875b25ca1bb63477f4297daa1505cf49bf02c94c3a93edd2523c90e
d3-quadtree/y.js         d99a8ff2f8a4609caf10532b4550f37889323c2d24ff0f0a1a8ab6a3d52e65dd
d3-dispatch/dispatch.js  fea3eae79697792aea368c145c8bc073b8d370936553b64197219968f8919c06
d3-dispatch/index.js     a7c286b3f6a04af34a5e5d197547fad4b62905c2a1d93a961184874481d87c78
d3-timer/index.js        42c8f4cb06c49e5e584616e91d14baec33fd0f15691facf7f19e7878446a8bfc
d3-timer/interval.js     4de2260b2620ce7fec943b88fc696b3a807ddadb312549e040706c731f963641
d3-timer/timeout.js      6ea61d840b63642d3575f564784e50f6b97c7afaa8d9b27cdbb6d87f546581e0
d3-timer/timer.js        7584c5ad7e3b7bc77b9cbe7063e4afe6831aa40412b4c4f5399bfad0566d2ab5
```

Regenerate with `node scripts/vendor-d3.mjs` (from `packages/ne-console/`).
