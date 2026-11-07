// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0
//
// THE SERVER'S STATED REASON, SAID TO A PERSON.
//
// A failed Run's `failure_reason` is a server-authored sentence, and parts of
// it are written in the validators' own closed vocabulary:
//
//   "…claim is not grounded by exact evidence [numeral-ungrounded x4,
//    phrase-recombination x1]."
//   "…Schema rejected: <whole reply> [json_invalid; opens=prose fences=0
//    objects=1 chars=274 sole=tool-call]."
//
// Those bracketed segments are REAL evidence — they name the exact validator
// findings — but they are evidence for whoever fixes the deployment, not prose
// for the person who asked the question. This module translates the two
// vocabularies the platform actually publishes into stated English and hands
// the caller the VERBATIM original for the existing "what the server said"
// disclosure. Nothing is deleted, and nothing is softened:
//
//   * every translation below says what the validator's code actually checks
//     (ne-govern/grounding.py finding classes; ne-runtime/model.py
//     `rejected_reply_shape`), never a friendlier meaning;
//   * a finding class this module does not know is stated AS a finding of that
//     named class — an honest generic, not a guess;
//   * the runtime's FLOOR sentence — the one a failure nobody explained
//     publishes, which names a Python exception class — states the absence of
//     an explanation and moves the class name into the disclosure;
//   * a reason that carries no known technical segment passes through
//     untouched, verbatim, with nothing to disclose twice.

export interface ExplainedReason {
  /** The stated line for the reader. Verbatim input when nothing translated. */
  readonly sentence: string;
  /** The exact original server sentence, when `sentence` rewrote any part of
   * it — the caller MUST surface this behind its evidence disclosure. `null`
   * when the sentence IS the verbatim original. */
  readonly technical: string | null;
}

/** ne-govern grounding finding classes, each said as the check it performs.
 * A count of 1 reads singular; the check itself is never restated softer. */
const GROUNDING_CLASSES: Readonly<Record<string, (n: number) => string>> = {
  "numeral-ungrounded": (n) =>
    n === 1
      ? "1 number in the answer is not present in the cited evidence"
      : `${n} numbers in the answer are not present in the cited evidence`,
  "numeral-unparseable": (n) =>
    n === 1
      ? "1 number in the answer could not be checked against the evidence"
      : `${n} numbers in the answer could not be checked against the evidence`,
  "phrase-recombination": (n) =>
    n === 1
      ? "1 passage stitches together wording that no single cited source contains"
      : `${n} passages stitch together wording that no single cited source contains`,
  "citation-out-of-range": (n) =>
    n === 1
      ? "1 citation marker points at no served source"
      : `${n} citation markers point at no served source`,
  "iri-ungrounded": (n) =>
    n === 1
      ? "1 link or identifier in the answer is not present in the cited evidence"
      : `${n} links or identifiers in the answer are not present in the cited evidence`,
  "vocab-ungrounded": (n) =>
    n === 1
      ? "1 term in the answer is not present in the cited evidence"
      : `${n} terms in the answer are not present in the cited evidence`,
  "vocab-ungrounded-factual": (n) =>
    n === 1
      ? "1 fact-bearing term in the answer is not present in the cited evidence"
      : `${n} fact-bearing terms in the answer are not present in the cited evidence`,
  "vocab-extended": (n) =>
    n === 1
      ? "1 relational term in the answer is not supported by the cited evidence"
      : `${n} relational terms in the answer are not supported by the cited evidence`,
  "relation-ungrounded": (n) =>
    n === 1
      ? "1 statement pairs fetched values in a relation that no cited row states"
      : `${n} statements pair fetched values in relations that no cited row states`,
};

const GROUNDING_TAIL =
  /claim is not grounded by exact evidence \[([a-z-]+ x\d+(?:, [a-z-]+ x\d+)*)\]/;

/** "class xN, class xM" -> stated clauses, honest-generic for unknown classes. */
function groundingClauses(findings: string): string {
  const clauses = findings.split(", ").map((finding) => {
    const match = /^([a-z-]+) x(\d+)$/.exec(finding);
    if (match === null) return finding; // not the expected grammar: verbatim
    const [, reason, count] = match;
    const say = GROUNDING_CLASSES[reason!];
    const n = Number(count);
    if (say !== undefined) return say(n);
    return n === 1 ? `1 finding of class “${reason}”` : `${n} findings of class “${reason}”`;
  });
  if (clauses.length === 1) return clauses[0]!;
  return `${clauses.slice(0, -1).join("; ")}; and ${clauses.at(-1)}`;
}

/** The reply-shape vocabulary (ne-runtime `rejected_reply_shape` and the
 * answer-select tokens), reduced to the one plain diagnosis it establishes. */
function schemaClause(tail: string): string {
  if (tail.includes("sole=tool-call") || tail.includes("obj=tool-call")) {
    return "The model answered with a tool call where its final answer belonged";
  }
  if (tail.includes("opens=fence")) {
    return "The model wrapped its answer in a code fence instead of the required structured form";
  }
  if (tail.includes("opens=prose")) {
    return "The model wrote prose instead of the required structured answer";
  }
  if (tail.includes("opens=array")) {
    return "The model sent a list where a single structured answer belonged";
  }
  if (/\[json_invalid[;\]]/.test(tail)) {
    return "The reply was not valid structured data in the required shape";
  }
  return "The reply did not fill the required answer fields correctly";
}

/** THE RUNTIME'S FLOOR SENTENCE, WHICH NAMES A PYTHON EXCEPTION CLASS.
 *
 * A failure whose raiser marked no prose `safe_reason` still publishes a
 * reason: ne-runtime/run_finalizer.py `derived_failure_reason` writes "The run
 * failed and the failing step published no explanation. Recorded failure class:
 * <ClassName>." That is the right thing for the RECORD to say — it is honest
 * about being derived and it discloses nothing the record did not hold — and
 * only about ten of the forty-odd classes in errors.py carry prose of their
 * own, so it is what most unanticipated failures publish.
 *
 * It reached the fault card's stated line unchanged, and that line is the
 * largest text on the screen. A reader watching a turn fail was shown a Python
 * class name as the platform's account of itself. The class is not deleted and
 * nothing here guesses at a cause the platform does not know: the sentence says
 * the step gave no reason, which is exactly what happened, and the verbatim
 * original — class name included — moves whole into the disclosure beside every
 * other raw server string. The class name is deliberately NOT read out of the
 * match: an exception type is evidence for whoever fixes the deployment, and
 * putting it in the reader's line is the defect, not the fix. */
const DERIVED_FLOOR =
  /^The run failed and the failing step published no explanation\. Recorded failure class: /;

/** What the reader is told when nothing explained the failure. */
export const UNEXPLAINED_FAILURE_SENTENCE =
  "The step that failed gave no reason for it — the record kept only the failure type.";

/**
 * Translate one published `failure_reason` for the stated line. The verbatim
 * original survives in `technical` whenever anything was rewritten.
 */
export function explainStatedReason(raw: string): ExplainedReason {
  // The grounding gate's finding list.
  const grounded = GROUNDING_TAIL.exec(raw);
  if (grounded !== null) {
    const sentence = raw.replace(GROUNDING_TAIL, groundingClauses(grounded[1]!));
    return { sentence, technical: raw };
  }
  // The answer-format rejection: the leading sentences are already plain
  // ("The model's final reply did not match this Run's required answer
  // format…"); the "Schema rejected: …" tail is the technical half.
  const schema = raw.indexOf("Schema rejected:");
  if (schema >= 0) {
    const lead = raw.slice(0, schema).trim();
    const sentence = `${lead === "" ? "" : `${lead} `}${schemaClause(raw.slice(schema))}.`;
    return { sentence, technical: raw };
  }
  // The malformed-tool-call variant that carries no schema list.
  if (raw.includes("[obj=tool-call]")) {
    return { sentence: raw.replace(/\s*\[obj=tool-call\]\s*/g, " ").trim(), technical: raw };
  }
  // The claim-binding rejection's attempt ledger and shape summary
  // ("… Attempts: no-span,no-span. [aw=154 sources=2 maxrun=1 floor=5]"):
  // the leading sentences are already stated English; the ledger becomes a
  // count and the closed-vocabulary bracket moves whole into the disclosure.
  const attempts = /\s*Attempts: ([a-z0-9-]+(?:,[a-z0-9-]+)*)\.(\s*\[[^\]]*\])?\s*$/.exec(raw);
  if (attempts !== null) {
    const spent = attempts[1]!.split(",").length;
    const lead = raw.slice(0, attempts.index).trim();
    const clause =
      spent === 1
        ? "1 bounded correction attempt was spent without correcting it."
        : `${spent} bounded correction attempts were spent without correcting it.`;
    return { sentence: `${lead === "" ? "" : `${lead} `}${clause}`, technical: raw };
  }
  // Any other trailing key=value bracket is a closed technical vocabulary the
  // reader cannot act on: the sentences before it stand, the bracket moves
  // whole into the disclosure. Nothing but the bracket is rewritten.
  const bracket = /\s*\[[a-z0-9_-]+=[^\]]*\]\.?\s*$/.exec(raw);
  if (bracket !== null) {
    const lead = raw.slice(0, bracket.index).trim();
    if (lead !== "") return { sentence: lead, technical: raw };
  }
  // The runtime's floor: nobody explained this failure, so the record holds a
  // class name and nothing else. Checked LAST because it is a whole-string
  // match against a constant head — no other arm above can claim it, and
  // putting it here keeps the arms in the order a reader of this file walks.
  if (DERIVED_FLOOR.test(raw)) {
    return { sentence: UNEXPLAINED_FAILURE_SENTENCE, technical: raw };
  }
  return { sentence: raw, technical: null };
}
