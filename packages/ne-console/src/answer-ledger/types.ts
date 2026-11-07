// Copyright (c) 2026 Ryan Junee.
// SPDX-License-Identifier: Apache-2.0

export type DriftClassification =
  "SAME" | "CHANGED-corpus" | "CHANGED-config" | "CHANGED-unexplained";

export interface CorpusChunkView {
  chunk_id: string;
  document_id: string;
  content_hash: string;
  document_version: number;
  document_href: string;
  /** ACL-safe title from the authoritative historical lineage version. */
  document_title: string | null;
}

export interface RootCauseView {
  answer_changed: boolean;
  corpus_changed: boolean;
  config_changed: boolean;
  corpus_diff_hrefs: string[];
  config_diff_href: string | null;
}

export interface AnswerEventView {
  /** 015A0-ENFORCEMENT: present on an ENFORCEMENT-era server. Anything but
   * `available` withholds the answer and everything derived from the sources
   * that produced it. */
  disclosure_status?: "available";
  event_id: string;
  occurred_at: string;
  answer: string;
  corpus_stamp: CorpusChunkView[];
  config_hash: string;
  served_model_id: string;
  trace_id: string;
  trace_href: string;
  classification: DriftClassification | null;
  root_cause: RootCauseView | null;
}

/**
 * 015A0-ENFORCEMENT: the withheld branch of one ledger event.
 *
 * It preserves the OWNED event id and timestamp — the ledger's own immutable
 * record that a re-ask happened — and nothing else. Answer, prompt excerpt,
 * labels, hashes, corpus stamps, citations, classification and root cause are
 * absent, and with them the copy and export affordances they fed.
 */
export interface WithheldAnswerEventView {
  disclosure_status: "content_withheld" | "retryable_unknown";
  event_id: string;
  occurred_at: string;
}

/** What the ledger may serve for one event. */
export type ServedAnswerEventView = AnswerEventView | WithheldAnswerEventView;

export interface PinnedQuestionView {
  question_id: string;
  owner_id: string;
  question: string;
  created_at: string;
  reask_schedule: null;
}

export interface AnswerLedgerView {
  question: PinnedQuestionView | null;
  events: ServedAnswerEventView[];
}
