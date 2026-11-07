# Public golden set — license & attribution

The two data files in this directory —

- `squad_corpus.jsonl` (159 paragraph documents)
- `squad_golden.jsonl` (100 questions with reference answers + relevance labels)

and the per-rubric label files under `calibration/` that are drawn from the same
source — are **derived test fixtures** built from the **Stanford Question
Answering Dataset (SQuAD) v1.1 development set**.

## Source

- Dataset: SQuAD v1.1 dev set
- Authors: Pranav Rajpurkar, Jian Zhang, Konstantin Lopyrev, Percy Liang —
  *"SQuAD: 100,000+ Questions for Machine Comprehension of Text"* (EMNLP 2016)
- Source URL: <https://rajpurkar.github.io/SQuAD-explorer/dataset/dev-v1.1.json>
- Source SHA-256 (the exact bytes these fixtures were generated from):
  `95aa6a52d5d6a735563366753ca50492a658031da74f301ac5238b03966972c9`
- Project homepage: <https://rajpurkar.github.io/SQuAD-explorer/>

## License

SQuAD v1.1 is distributed under the **Creative Commons Attribution-ShareAlike
4.0 International License (CC BY-SA 4.0)** —
<https://creativecommons.org/licenses/by-sa/4.0/>.

CC BY-SA 4.0 **permits redistribution** (including in a commercial product), with
attribution and share-alike. It is **not** a non-commercial (NC) or
research-only license, so it passes the platform's C4 license gate for a
committed test-data fixture. (MS MARCO — research/non-commercial — and mixed-
license BEIR subsets were rejected for this reason.)

**Scope note:** CC BY-SA 4.0 governs ONLY the derived *data* in the two JSONL
files and the SQuAD-derived `calibration/*_labels.jsonl` rows. It does **not**
apply to the platform source code (including `generate_squad_subset.py`), which
is licensed under `Apache-2.0`. The data files are test fixtures,
not part of the shipped platform.

## Regenerating

The fixtures are a deterministic, pure function of the source file above:

```
python packages/ne-eval/goldensets/public/generate_squad_subset.py
```

Re-running against the recorded-SHA-256 source reproduces both JSONL files
byte-for-byte (`tests/test_public_golden_set.py` guards this).
