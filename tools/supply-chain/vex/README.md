# OpenVEX exception process

Trivy and Grype both consume [OpenVEX](https://github.com/openvex/spec) documents
via `--vex <file>` to suppress vulnerability findings that do not actually affect
our shipped software. VEX is the ONLY sanctioned way to silence a scanner finding
— we never lower a severity threshold or disable a scanner to get a green build.

**Gate policy:** the scans fail on HIGH/CRITICAL findings that have a fix
available (`trivy --ignore-unfixed`, `grype --only-fixed`). A HIGH/CRITICAL
finding with no fix is report-only: it does not fail the build, needs no VEX
statement, and is listed in the `vuln-scan` job summary and the `vuln-reports`
artifact. VEX statements are therefore for fixable findings that do not affect
us, plus any unfixed finding we choose to document.

## Ledgers

Two ledgers live here and they are never mixed — a statement in one can never
silence a finding the other scan reported:

| Ledger | Read by | Enforced by |
| --- | --- | --- |
| `openvex.json` | the DIRECTORY scans only (`trivy fs` and `grype dir:.`) | `vex_dualkey_gate.py` |
| `ne-postgres-age.openvex.json` | the Grype scan of the BUILT `ne-postgres-age` image only | `image_vex_gate.py`, together with `ne-postgres-age.reachability.json` |

The Trivy image scan never reads VEX at all: Trivy matches on the Wolfi secdb
with `--ignore-unfixed`, so it goes red exactly when a fix exists. Neither ledger is passed to the
other's scans.

## Statuses

Each OpenVEX statement carries a `status` for a `(vulnerability, product)` pair:

| status | meaning | required field |
| --- | --- | --- |
| `not_affected` | the vuln does not affect us | `justification` (an OpenVEX justification enum) |
| `false_positive` | the scanner mis-identified the component | rationale in `impact_statement` |
| `fixed` | remediated in the referenced version | — |
| `affected` | genuinely affected — do NOT use to silence; fix it | `action_statement` |

Valid `not_affected` justifications (OpenVEX):
`component_not_present`, `vulnerable_code_not_present`,
`vulnerable_code_not_in_execute_path`, `vulnerable_code_cannot_be_controlled_by_adversary`,
`inline_mitigations_already_exist`.

## Process

1. A scanner flags CVE-XXXX. Triage it against the actual code path.
2. If genuinely not-affected / false-positive / already-fixed, add a statement to
   the ledger of the scan that reported it (copy the shape from
   `openvex.template.json`) with a concrete `justification`/`impact_statement`
   and the affected component's PURL.
3. When Trivy and Grype key the same vulnerability under different ids (CVE vs
   GHSA vs PYSEC), add ONE STATEMENT PER SCANNER-REPORTED ID — each with the
   exact reported id as `vulnerability.name` and the sibling ids as `aliases`
   (see the CVE-2026-4372 / GHSA-29pf-2h5f-8g72 pair in `openvex.json` as the
   template). A single-keyed statement is not enough: `vex_dualkey_gate.py`
   matches on exact `vulnerability.name` only, never on aliases, so CI enforces
   this discipline — a single-keyed addition fails the `vuln-scan` job closed.
4. Land it via a **reviewed PR** — a VEX statement is a security assertion and gets
   the same scrutiny as code.
5. The `supply-chain.yml` workflow passes `--vex tools/supply-chain/vex/openvex.json`
   to the Trivy and Grype DIRECTORY scans only when that file exists and is
   non-empty. The Grype IMAGE step always passes
   `--vex tools/supply-chain/vex/ne-postgres-age.openvex.json`, and the
   `image_vex_gate.py` step runs immediately before it in the same job.

We never fabricate statements. An empty exception set means every finding stands.

## Image-scoped statements

`ne-postgres-age.openvex.json` carries extra rules, enforced by
`image_vex_gate.py` rather than by diligence:

- The product is the VERBATIM `matches[].artifact.purl` Grype reports on the
  BUILT image — version-pinned, `pkg:apk/`. Never hand-written, never a guessed
  namespace or qualifier set.
- One statement per scanner-reported id; aliases never count, exactly as for the
  directory ledger.
- Every statement needs a `ne-postgres-age.reachability.json` entry
  `{needles, allowed_files}` stating which files may legitimately contain the
  vulnerable symbol.

The gate's five checks: (a) the set of regular files in a `docker export` of the
image whose bytes contain a needle must EQUAL `allowed_files` — an extra file
fails and a missing one fails; (b) every raw un-VEX'd HIGH-and-above Grype
`(id, purl)` with a fix available (`fix.state == "fixed"`) must be in the ledger
`(name, @id)` set, and every ledger pair must match a raw finding (fixed or not);
an unfixed, unledgered finding is printed as `IMAGE-VEX report-only:` and does
not fail;
(c) any VEX'd finding Grype reports with `fix.state == "fixed"` fails; (d)
ledger names and reachability keys match one to one; (e) every statement is
`not_affected` / `vulnerable_code_not_in_execute_path` with exactly one
version-pinned `pkg:apk/` product and a non-blank `impact_statement`.

`allowed_files` is never widened, and a statement is never added here, without a
new SPEC §6.1.1 arbiter ruling.

A statement comes OUT when any of three removal conditions fires: (1) Grype
reports `fix.state == fixed` -- bump the base, then delete the statement and its
reachability entry (check (c) forces this); (2) an advisory revision moves the
defect outside the needle symbol, which makes the needle unsound; (3) the distro
feed records a fix at or below the pinned package version, so Grype no longer
reports the `(id, purl)` pair at all -- delete the statement and its
reachability entry without any bump (check (b) fails it as a `stale statement`
until you do).

An empty ledger (`"statements": []` with a `{}` reachability map) is a valid and
honest state: the gate still runs on every build and fails closed on the first
unledgered HIGH-and-above finding that has a fix available. It is the ledger's current state.

Residual, stated because no test covers it: a stripped, statically linked caller
of the symbol carries no symbol name and is invisible to the byte probe.

## Open triage note — 2026-09-02 (uncarded; found during CVE-2026-9856 triage)

The six pre-existing statements for `pkg:pypi/transformers@4.57.6`
(CVE-2026-4372 / CVE-2026-5241 / CVE-2026-1839 / CVE-2025-14929 and their
GHSA/PYSEC twins) all claim `vulnerable_code_not_in_execute_path`. During the
CVE-2026-9856 triage it was established that the model-export sidecar's
`optimum.exporters.onnx.main_export()` call DOES transitively execute
`transformers` save paths (optimum-onnx 0.1.0 `exporters/onnx/convert.py`
lines 1029/1035/1040 -> optimum 2.1.0 `utils/save_utils.py` line 92,
`preprocessor.save_pretrained()` including the tokenizer). Whether each of
those six CVEs' vulnerable code lies on a path the sidecar executes has NOT
been re-verified against this finding — if any does, its justification needs
to move to `vulnerable_code_cannot_be_controlled_by_adversary` (the pinned
`97b0c614` checkpoint reasoning used for CVE-2026-9856) or be re-triaged.
Deliberately NOT re-attested on 2026-09-02 (deploy night); re-verify each
statement's justification against the actual reachable path before the next
attestation update.
