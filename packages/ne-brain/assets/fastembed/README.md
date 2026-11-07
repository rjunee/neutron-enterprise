# Vendored FastEmbed BM25 model assets (`Qdrant/bm25`)

This directory vendors the [`Qdrant/bm25`](https://huggingface.co/Qdrant/bm25)
FastEmbed model assets **in-repo** so CI's Checkpoint-C retrieval baseline
(NE-CKC6) loads them **fully offline** — no HuggingFace fetch at all.

## Provenance

- **Source:** https://huggingface.co/Qdrant/bm25
- **Revision:** `e499a1f8d6bec960aab5533a0941bf914e70faf9` (HF `main`)
- **Fetched:** 2026-07-03 · **Verified against the HF model card / API `.sha`:** 2026-07-06
- **License:** Apache-2.0 (model-card `license: apache-2.0` tag)

## Contents

`models--Qdrant--bm25/` is a verbatim
[`huggingface_hub`](https://github.com/huggingface/huggingface_hub) on-disk cache
tree (`refs/`, `snapshots/<sha>/`, `blobs/`, `trees/`, `files_metadata.json`).
The snapshot is `config.json` plus 17 per-language stopword lists
(`english.txt`, `french.txt`, …). BM25 is a lexical model — these vocab/stopword
files are the whole payload (~90 KB of content; ~160 KB dereferenced). The
snapshot symlinks are **dereferenced** (plain files, no git-lfs) so no symlink
bitrot can enter git — enforced by
`packages/ne-brain/tests/test_vendored_bm25_assets.py::test_vendored_tree_has_no_symlinks`.

## Why vendored

On 2026-07-06 the GH Actions model cache key `fastembed-<os>-qdrant-bm25-v1` was
LRU/expiry-evicted, so cold CI runs cold-downloaded the model from HuggingFace and
hit `429 Too Many Requests` (shared runner IPs) — fastembed then raised
`Could not load model Qdrant/bm25 from any source`, ERRORing the fail-closed
Checkpoint-C baseline (known fastembed issue qdrant/fastembed #526, #560). Caching
alone cannot survive eviction. Vendoring makes the load **eviction- and
429-immune** and matches the platform's C11 air-gap / no-egress posture.

## How CI consumes it

`.github/workflows/ci.yml` stages this tree into `FASTEMBED_CACHE_PATH` and sets
`HF_HUB_OFFLINE=1` job-wide, so the BM25 load resolves from the local cache ONLY
and any reintroduced HF dependency fails loudly instead of flaking on 429.
NE-CKC6 stays fail-closed: the gate still performs a REAL model load — only the
model **source** moved from network to repo.

## Update trigger

Only if `ne/brain/sparse.py` `_DEFAULT_MODEL` changes (it is code-pinned to
`Qdrant/bm25`). To refresh: re-download the model, re-vendor with `cp -RL` (to
dereference symlinks), update the revision above and the `packages/ne-brain/NOTICE`
entry, and bump the staging step in `ci.yml`.
