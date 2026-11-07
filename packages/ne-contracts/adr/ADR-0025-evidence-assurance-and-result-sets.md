# 0025 — Evidence assurance and source-bound Tool result sets

## Status

Accepted (2026-08-22; PRE-011C, SPEC §13).

## Context

A governed search or graph read can use the existing single protected Tool-result
reference. A verified T3 row can have several independently admitted source
contributors, however, and SPEC requires one immutable EvidenceRecord and locator
for each contributor while delivering one protected Tool result. Collapsing those
contributors into one record loses the complete row provenance. Reusing graph
element assurance would also conflate how a graph fact was produced with how an
EvidenceRecord supports an answer.

ADR-0024 and contract version 1.21.0 are reserved by PRE-015A1. This additive
change therefore takes the next identifiers even when applied to a base where the
reserved change has not merged yet.

## Decision

1. Add the closed `EvidenceAssurance` values `direct`, `inferred`, and
   `extracted`. This vocabulary is independent of graph element assurance
   (`source`, `confirmed`, `extracted`); neither enum is translated into the
   other.
2. Add `SourceBoundToolResultSetRef` as a subtype of the existing
   `SourceBoundToolResultRef`. It carries a bounded, ordered, unique non-empty
   tuple of opaque EvidenceRecord ids for one protected result.
3. Preserve `SourceBoundToolResultRef` byte-for-byte for existing single-record
   producers and consumers. Resolver methods accept both shapes.
4. The encrypted pending-result row remains the source of protected bytes and
   complete authority stamps. The runtime verifies that a set reference's ordered
   evidence ids match the ids authenticated inside that row before disclosure.

## Consequences

Verified producers can persist a complete contributor set atomically without
duplicating protected result bytes. Missing, reordered, duplicated, or swapped
members fail closed. Consumers that only understand the original single-ref shape
remain source-compatible. This is an additive MINOR contract bump to 1.22.0.
